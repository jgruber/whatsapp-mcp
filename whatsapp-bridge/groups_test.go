package main

import (
	"strings"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

func TestParticipantsToJIDs(t *testing.T) {
	tests := []struct {
		name    string
		in      []string
		wantErr string // substring the error must contain, or "" for success
		wantN   int
	}{
		{
			name:  "digits become s.whatsapp.net JIDs",
			in:    []string{"14155551234", "14155559999"},
			wantN: 2,
		},
		{
			name:  "fully-qualified user JIDs are accepted as-is",
			in:    []string{"14155551234@s.whatsapp.net"},
			wantN: 1,
		},
		{
			name:  "blank entries are skipped",
			in:    []string{"14155551234", "", "  "},
			wantN: 1,
		},
		{
			name:    "group JID is rejected (not a user)",
			in:      []string{"120363043745024044@g.us"},
			wantErr: "must be a user JID",
		},
		{
			name:  "lid JID is accepted (hidden-user identity)",
			in:    []string{"77648730558591@lid"},
			wantN: 1,
		},
		{
			name:    "non-numeric phone is rejected",
			in:      []string{"not a jid"},
			wantErr: "phone must be digits only",
		},
		{
			name:    "empty list is rejected",
			in:      []string{"", "   "},
			wantErr: "participants list is empty",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, err := participantsToJIDs(tc.in)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("want error containing %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if len(got) != tc.wantN {
				t.Fatalf("want %d jids, got %d: %v", tc.wantN, len(got), got)
			}
			for _, j := range got {
				if j.Server != types.DefaultUserServer && j.Server != types.HiddenUserServer {
					t.Fatalf("participant %s has non-user server %q", j, j.Server)
				}
			}
		})
	}
}

func TestParseGroupJID(t *testing.T) {
	tests := []struct {
		in      string
		wantErr string
	}{
		{"120363043745024044", ""},
		{"120363043745024044@g.us", ""},
		{"120363043745024044@s.whatsapp.net", "not a group jid"},
		{"", "missing group jid"},
		{"not@a@jid", "not a group jid"},
	}
	for _, tc := range tests {
		t.Run(tc.in, func(t *testing.T) {
			jid, err := parseGroupJID(tc.in)
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("want error %q, got %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if jid.Server != types.GroupServer {
				t.Fatalf("want group server, got %q", jid.Server)
			}
		})
	}
}
