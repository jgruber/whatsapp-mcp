package main

// Group management REST endpoints.
//
// Thin wrappers around whatsmeow's group calls, exposed so the MCP server
// can offer list/info/create/update/invite-link tools. All endpoints are
// wrapped in the same auth() middleware as the rest of /api/* and
// return bodies shaped for direct JSON consumption by the MCP tools.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

// groupParticipantJSON is a stable JSON shape for a single group member.
type groupParticipantJSON struct {
	JID          string `json:"jid"`
	PhoneNumber  string `json:"phone_number,omitempty"`
	DisplayName  string `json:"display_name,omitempty"`
	IsAdmin      bool   `json:"is_admin"`
	IsSuperAdmin bool   `json:"is_super_admin"`
}

// groupJSON is the response shape for group endpoints.
type groupJSON struct {
	JID              string                 `json:"jid"`
	Name             string                 `json:"name"`
	Topic            string                 `json:"topic,omitempty"`
	OwnerJID         string                 `json:"owner_jid,omitempty"`
	ParticipantCount int                    `json:"participant_count"`
	Participants     []groupParticipantJSON `json:"participants,omitempty"`
	CreatedAt        string                 `json:"created_at,omitempty"`
	IsAnnounce       bool                   `json:"is_announce"`
	IsLocked         bool                   `json:"is_locked"`
	IsEphemeral      bool                   `json:"is_ephemeral"`
}

func groupInfoToJSON(g *types.GroupInfo, includeParticipants bool) groupJSON {
	out := groupJSON{
		JID:              g.JID.String(),
		Name:             g.GroupName.Name,
		Topic:            g.GroupTopic.Topic,
		OwnerJID:         jidString(g.OwnerJID),
		ParticipantCount: len(g.Participants),
		IsAnnounce:       g.GroupAnnounce.IsAnnounce,
		IsLocked:         g.GroupLocked.IsLocked,
		IsEphemeral:      g.GroupEphemeral.IsEphemeral,
	}
	if !g.GroupCreated.IsZero() {
		out.CreatedAt = g.GroupCreated.UTC().Format(time.RFC3339)
	}
	if includeParticipants {
		out.Participants = make([]groupParticipantJSON, 0, len(g.Participants))
		for _, p := range g.Participants {
			out.Participants = append(out.Participants, groupParticipantJSON{
				JID:          p.JID.String(),
				PhoneNumber:  p.PhoneNumber.User,
				DisplayName:  p.DisplayName,
				IsAdmin:      p.IsAdmin,
				IsSuperAdmin: p.IsSuperAdmin,
			})
		}
	}
	return out
}

func jidString(j types.JID) string {
	if j.IsEmpty() {
		return ""
	}
	return j.String()
}

// userJIDPattern matches a phone-style user part: all digits, optionally
// followed by a `:device` suffix (e.g. "14155551234:12"). Rejects garbage
// like "not a jid" that would otherwise become a cursed JID.
var userJIDPattern = regexp.MustCompile(`^[0-9]+(:[0-9]+)?$`)

// participantsToJIDs normalizes a list of phone numbers or JIDs to
// []types.JID. Blank entries are skipped. Rejected: non-numeric phone
// inputs, group JIDs (`@g.us`), and anything that is neither a user
// (`@s.whatsapp.net`) nor a LID (`@lid`) — passing those to whatsmeow's
// group-participant APIs would surface as an opaque WA error.
func participantsToJIDs(raw []string) ([]types.JID, error) {
	out := make([]types.JID, 0, len(raw))
	for _, s := range raw {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		var jid types.JID
		var err error
		if strings.Contains(s, "@") {
			jid, err = types.ParseJID(s)
			if err != nil || jid.User == "" || jid.Server == "" {
				return nil, fmt.Errorf("invalid participant %q", s)
			}
		} else {
			if !userJIDPattern.MatchString(s) {
				return nil, fmt.Errorf("invalid participant %q (phone must be digits only)", s)
			}
			jid = types.NewJID(s, types.DefaultUserServer)
		}
		if jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer {
			return nil, fmt.Errorf("participant %q must be a user JID (@%s or @%s), got @%s",
				s, types.DefaultUserServer, types.HiddenUserServer, jid.Server)
		}
		out = append(out, jid)
	}
	if len(out) == 0 {
		return nil, errors.New("participants list is empty")
	}
	return out, nil
}

// parseGroupJID extracts the group JID from the path segment after
// /api/groups/. The segment may be the user part only ("12345-67890") or a
// fully-qualified JID ("12345-67890@g.us").
func parseGroupJID(raw string) (types.JID, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return types.EmptyJID, errors.New("missing group jid")
	}
	if !strings.Contains(raw, "@") {
		raw = raw + "@" + types.GroupServer
	}
	jid, err := types.ParseJID(raw)
	if err != nil {
		return types.EmptyJID, err
	}
	if jid.Server != types.GroupServer {
		return types.EmptyJID, fmt.Errorf("not a group jid: %s", jid.String())
	}
	return jid, nil
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

// maxGroupBodyBytes caps JSON request bodies on the group endpoints. 1 MiB
// is far more than any sane participants list and plenty of margin for the
// name/action payloads; the cap exists so an authenticated caller can't
// drive the bridge OOM with a 2 GB body.
const maxGroupBodyBytes = 1 << 20

func decodeGroupBody(w http.ResponseWriter, r *http.Request, dst any) error {
	r.Body = http.MaxBytesReader(w, r.Body, maxGroupBodyBytes)
	return json.NewDecoder(r.Body).Decode(dst)
}

// registerGroupRoutes wires the five group endpoints onto the shared mux.
// The auth wrapper is passed in so this file does not need to know how
// authentication is enforced.
func registerGroupRoutes(mux *http.ServeMux, auth func(http.HandlerFunc) http.HandlerFunc, client *whatsmeow.Client) {
	// GET /api/groups — joined groups the account is a member of.
	mux.HandleFunc("/api/groups", auth(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			handleListGroups(w, r, client)
		case http.MethodPost:
			handleCreateGroup(w, r, client)
		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	}))

	// Everything under /api/groups/… is dispatched here.
	mux.HandleFunc("/api/groups/", auth(func(w http.ResponseWriter, r *http.Request) {
		rest := strings.TrimPrefix(r.URL.Path, "/api/groups/")
		if rest == "" {
			http.Error(w, "group jid required", http.StatusBadRequest)
			return
		}
		segments := strings.SplitN(rest, "/", 2)
		jidPart := segments[0]
		action := ""
		if len(segments) == 2 {
			action = segments[1]
		}
		jid, err := parseGroupJID(jidPart)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		switch {
		case action == "" && r.Method == http.MethodGet:
			handleGroupInfo(w, r, client, jid)
		case action == "participants" && r.Method == http.MethodPatch:
			handleUpdateParticipants(w, r, client, jid)
		case action == "invite-link" && (r.Method == http.MethodGet || r.Method == http.MethodPost):
			handleInviteLink(w, r, client, jid)
		default:
			http.Error(w, "no such group endpoint", http.StatusNotFound)
		}
	}))
}

func handleListGroups(w http.ResponseWriter, _ *http.Request, client *whatsmeow.Client) {
	groups, err := client.GetJoinedGroups(context.Background())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	out := make([]groupJSON, 0, len(groups))
	for _, g := range groups {
		out = append(out, groupInfoToJSON(g, false))
	}
	writeJSON(w, http.StatusOK, map[string]any{"groups": out})
}

func handleGroupInfo(w http.ResponseWriter, _ *http.Request, client *whatsmeow.Client, jid types.JID) {
	info, err := client.GetGroupInfo(context.Background(), jid)
	if err != nil {
		status := http.StatusInternalServerError
		// whatsmeow surfaces WA's own IQ error for a missing group; the exact
		// error type varies across versions, so match on the message. A
		// non-NotFound error (e.g. a disconnected socket) stays as 500 so the
		// client can distinguish a real not-found from a transient failure.
		if msg := strings.ToLower(err.Error()); strings.Contains(msg, "not found") ||
			strings.Contains(msg, "no such group") || strings.Contains(msg, "item-not-found") {
			status = http.StatusNotFound
		}
		writeJSON(w, status, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, groupInfoToJSON(info, true))
}

func handleCreateGroup(w http.ResponseWriter, r *http.Request, client *whatsmeow.Client) {
	var req struct {
		Name         string   `json:"name"`
		Participants []string `json:"participants"`
	}
	if err := decodeGroupBody(w, r, &req); err != nil {
		http.Error(w, "invalid JSON body: "+err.Error(), http.StatusBadRequest)
		return
	}
	name := strings.TrimSpace(req.Name)
	if name == "" {
		http.Error(w, "name is required", http.StatusBadRequest)
		return
	}
	jids, err := participantsToJIDs(req.Participants)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	info, err := client.CreateGroup(context.Background(), whatsmeow.ReqCreateGroup{
		Name:         name,
		Participants: jids,
	})
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, groupInfoToJSON(info, true))
}

func handleUpdateParticipants(w http.ResponseWriter, r *http.Request, client *whatsmeow.Client, jid types.JID) {
	var req struct {
		Action       string   `json:"action"`
		Participants []string `json:"participants"`
	}
	if err := decodeGroupBody(w, r, &req); err != nil {
		http.Error(w, "invalid JSON body: "+err.Error(), http.StatusBadRequest)
		return
	}
	var action whatsmeow.ParticipantChange
	switch strings.ToLower(strings.TrimSpace(req.Action)) {
	case "add":
		action = whatsmeow.ParticipantChangeAdd
	case "remove":
		action = whatsmeow.ParticipantChangeRemove
	case "promote":
		action = whatsmeow.ParticipantChangePromote
	case "demote":
		action = whatsmeow.ParticipantChangeDemote
	default:
		http.Error(w, "action must be one of add|remove|promote|demote", http.StatusBadRequest)
		return
	}
	jids, err := participantsToJIDs(req.Participants)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	results, err := client.UpdateGroupParticipants(context.Background(), jid, jids, action)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	out := make([]map[string]any, 0, len(results))
	for _, p := range results {
		out = append(out, map[string]any{
			"jid":    p.JID.String(),
			"status": p.Error, // 0 on success; non-zero is the WA status code
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"action":  req.Action,
		"results": out,
	})
}

func handleInviteLink(w http.ResponseWriter, r *http.Request, client *whatsmeow.Client, jid types.JID) {
	reset := strings.EqualFold(r.URL.Query().Get("reset"), "true")
	link, err := client.GetGroupInviteLink(context.Background(), jid, reset)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"invite_link": link})
}
