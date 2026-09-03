package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHandshakeAdvertisesFrozenABIAndCapabilities(t *testing.T) {
	var out bytes.Buffer
	if err := runHandshake(&out); err != nil {
		t.Fatal(err)
	}
	var h handshake
	dec := json.NewDecoder(&out)
	dec.DisallowUnknownFields()
	if err := dec.Decode(&h); err != nil {
		t.Fatal(err)
	}
	if h.Schema != handshakeSchema || h.ABI != pluginABI || h.PluginID != pluginID {
		t.Fatalf("unexpected handshake identity: %+v", h)
	}
	wantCaps := map[string]bool{capTLSServer: false, capStampEd25519: false, capVerifyEd25519: false}
	for _, cap := range h.Capabilities {
		if _, ok := wantCaps[cap]; !ok {
			t.Fatalf("unexpected capability %q", cap)
		}
		wantCaps[cap] = true
	}
	for cap, seen := range wantCaps {
		if !seen {
			t.Fatalf("missing capability %q", cap)
		}
	}
	if h.Build.Static == h.Build.CGO {
		t.Fatalf("static/cgo descriptor inconsistent: %+v", h.Build)
	}
}

func TestSecureStampRoundTripAndTamper(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte("canonical receipt bytes"))
	stamp := makeStamp(digest, priv)
	valid, err := verifyStamp(digest, stamp, pub)
	if err != nil || !valid {
		t.Fatalf("valid stamp rejected: valid=%v err=%v", valid, err)
	}

	tampered := sha256.Sum256([]byte("canonical receipt bytes!"))
	valid, err = verifyStamp(tampered, stamp, pub)
	if err != nil {
		t.Fatal(err)
	}
	if valid {
		t.Fatal("tampered payload accepted")
	}
}

func TestSecureStampTrustedKeyPolicy(t *testing.T) {
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	otherPub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte("payload"))
	stamp := makeStamp(digest, priv)
	valid, err := verifyStamp(digest, stamp, otherPub)
	if err != nil {
		t.Fatal(err)
	}
	if valid {
		t.Fatal("stamp accepted under wrong trusted public key")
	}
}

func TestStampHexMustBeCanonical(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256([]byte("payload"))
	stamp := makeStamp(digest, priv)
	stamp.SignatureHex = strings.ToUpper(stamp.SignatureHex)
	if _, err := verifyStamp(digest, stamp, pub); err == nil {
		t.Fatal("uppercase/noncanonical stamp hex was accepted")
	}
}

func TestDigestReaderLimit(t *testing.T) {
	if _, err := digestReader(strings.NewReader("12345"), 5); err != nil {
		t.Fatalf("exact limit rejected: %v", err)
	}
	if _, err := digestReader(strings.NewReader("123456"), 5); err == nil {
		t.Fatal("oversized input accepted")
	}
}

func TestRunServeRejectsRelativeSecretPathsBeforeFileLoad(t *testing.T) {
	err := runServe([]string{
		"--listen", "127.0.0.1:0",
		"--upstream", "http://127.0.0.1:8790",
		"--cert", "cert.pem",
		"--key", "/run/secrets/key.pem",
	})
	if err == nil || !strings.Contains(err.Error(), "absolute paths") {
		t.Fatalf("relative TLS certificate path was not rejected: %v", err)
	}
}

func TestRunStampAndVerifyRejectRelativeSecretPaths(t *testing.T) {
	var out bytes.Buffer
	if err := runStamp([]string{"--key", "stamp-key.pem"}, strings.NewReader("x"), &out); err == nil || !strings.Contains(err.Error(), "absolute path") {
		t.Fatalf("relative stamp key was not rejected: %v", err)
	}
	out.Reset()
	if err := runVerify([]string{"--stamp", "stamp.json"}, strings.NewReader("x"), &out); err == nil || !strings.Contains(err.Error(), "absolute path") {
		t.Fatalf("relative stamp file was not rejected: %v", err)
	}
}

func TestWritePEMFileForceReassertsPrivateMode(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "private.pem")
	if err := os.WriteFile(path, []byte("old"), 0o666); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o666); err != nil {
		t.Fatal(err)
	}
	if err := writePEMFile(path, "PRIVATE KEY", []byte{1, 2, 3, 4}, 0o600, true); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := info.Mode().Perm(); got != 0o600 {
		t.Fatalf("forced private-key replacement kept wrong mode: got %04o want 0600", got)
	}
}

func TestRunGenKeyRejectsRelativeOutputPaths(t *testing.T) {
	err := runGenKey([]string{"--private", "private.pem", "--public", "public.pem"})
	if err == nil || !strings.Contains(err.Error(), "absolute paths") {
		t.Fatalf("relative key outputs were not rejected: %v", err)
	}
}
