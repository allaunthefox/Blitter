package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	pluginABI        = "mathpunch.blitter-security-plugin.v1"
	handshakeSchema  = "mathpunch.blitter-security-plugin-handshake.v1"
	pluginID         = "mathpunch-go-security-plugin"
	pluginVersion    = "0.1.0"
	stampSchema      = "mathpunch.secure-stamp.v1"
	verifySchema     = "mathpunch.secure-stamp-verification.v1"
	stampAlgorithm   = "ed25519-sha256-domain-v1"
	stampDomain      = "mathpunch-secure-stamp-v1\x00"
	maxStampBytes    = int64(64 << 20)
	defaultMaxBody   = int64(2 << 20)
	capTLSServer     = "tls.server.http1.reverse-proxy"
	capStampEd25519  = "stamp.ed25519.sha256"
	capVerifyEd25519 = "verify.ed25519.sha256"
)

type handshake struct {
	Schema        string         `json:"schema"`
	ABI           string         `json:"abi"`
	PluginID      string         `json:"plugin_id"`
	PluginVersion string         `json:"plugin_version"`
	Capabilities  []string       `json:"capabilities"`
	Build         handshakeBuild `json:"build"`
}

type handshakeBuild struct {
	GOOS   string `json:"goos"`
	GOARCH string `json:"goarch"`
	CGO    bool   `json:"cgo"`
	Static bool   `json:"static"`
}

type secureStamp struct {
	Schema        string `json:"schema"`
	Algorithm     string `json:"algorithm"`
	PayloadSHA256 string `json:"payload_sha256"`
	PublicKeyHex  string `json:"public_key_hex"`
	SignatureHex  string `json:"signature_hex"`
}

type verifyResult struct {
	Schema        string `json:"schema"`
	Valid         bool   `json:"valid"`
	PayloadSHA256 string `json:"payload_sha256"`
	PublicKeyHex  string `json:"public_key_hex"`
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}

	var err error
	switch os.Args[1] {
	case "handshake":
		err = runHandshake(os.Stdout)
	case "serve":
		err = runServe(os.Args[2:])
	case "stamp":
		err = runStamp(os.Args[2:], os.Stdin, os.Stdout)
	case "verify":
		err = runVerify(os.Args[2:], os.Stdin, os.Stdout)
	case "gen-key":
		err = runGenKey(os.Args[2:])
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "blitter-security-plugin:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: blitter-security-plugin <handshake|serve|stamp|verify|gen-key> [options]")
}

func runHandshake(w io.Writer) error {
	h := handshake{
		Schema:        handshakeSchema,
		ABI:           pluginABI,
		PluginID:      pluginID,
		PluginVersion: pluginVersion,
		Capabilities:  []string{capTLSServer, capStampEd25519, capVerifyEd25519},
		Build: handshakeBuild{
			GOOS:   runtime.GOOS,
			GOARCH: runtime.GOARCH,
			CGO:    cgoEnabled,
			Static: !cgoEnabled,
		},
	}
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	return enc.Encode(h)
}

func runServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	listen := fs.String("listen", "", "TLS listen address, for example 0.0.0.0:443")
	upstreamRaw := fs.String("upstream", "", "explicit HTTP upstream URL")
	certFile := fs.String("cert", "", "PEM certificate/fullchain file")
	keyFile := fs.String("key", "", "PEM private-key file")
	maxBody := fs.Int64("max-body-bytes", defaultMaxBody, "maximum proxied request body bytes")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return errors.New("serve: unexpected positional arguments")
	}
	if *listen == "" || *upstreamRaw == "" || *certFile == "" || *keyFile == "" {
		return errors.New("serve: --listen, --upstream, --cert, and --key are required")
	}
	if !filepath.IsAbs(*certFile) || !filepath.IsAbs(*keyFile) {
		return errors.New("serve: --cert and --key must be absolute paths")
	}
	if *maxBody <= 0 {
		return errors.New("serve: --max-body-bytes must be positive")
	}
	if _, err := tls.LoadX509KeyPair(*certFile, *keyFile); err != nil {
		return fmt.Errorf("serve: certificate/key validation failed: %w", err)
	}

	upstream, err := url.Parse(*upstreamRaw)
	if err != nil {
		return fmt.Errorf("serve: invalid upstream URL: %w", err)
	}
	if upstream.Scheme != "http" || upstream.Host == "" || upstream.User != nil {
		return errors.New("serve: V1 upstream must be an explicit http:// URL with host and no userinfo")
	}

	proxy := httputil.NewSingleHostReverseProxy(upstream)
	baseDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		originalHost := req.Host
		baseDirector(req)
		req.Header.Set("X-Forwarded-Proto", "https")
		if originalHost != "" {
			req.Header.Set("X-Forwarded-Host", originalHost)
		}
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, proxyErr error) {
		log.Printf("proxy error: %v", proxyErr)
		http.Error(w, "blitter security proxy upstream failure", http.StatusBadGateway)
	}
	proxy.Transport = &http.Transport{
		Proxy:                 nil,
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     false,
		MaxIdleConns:          32,
		IdleConnTimeout:       60 * time.Second,
		TLSHandshakeTimeout:   5 * time.Second,
		ExpectContinueTimeout: time.Second,
	}

	handler := http.MaxBytesHandler(proxy, *maxBody)
	server := &http.Server{
		Addr:              *listen,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      5 * time.Minute,
		IdleTimeout:       60 * time.Second,
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
			NextProtos:  []string{"http/1.1"},
		},
		// V1 advertises HTTP/1.x only. An HTTP/2-capable replacement plugin may
		// expose a distinct capability without changing the blitter core.
		TLSNextProto: make(map[string]func(*http.Server, *tls.Conn, http.Handler)),
	}
	log.Printf("serving TLS on %s -> %s", *listen, upstream.String())
	return server.ListenAndServeTLS(*certFile, *keyFile)
}

func runStamp(args []string, input io.Reader, output io.Writer) error {
	fs := flag.NewFlagSet("stamp", flag.ContinueOnError)
	keyFile := fs.String("key", "", "Ed25519 PKCS#8 PEM private key")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 || *keyFile == "" {
		return errors.New("stamp: --key is required and positional arguments are not accepted")
	}
	if !filepath.IsAbs(*keyFile) {
		return errors.New("stamp: --key must be an absolute path")
	}
	key, err := loadPrivateKey(*keyFile)
	if err != nil {
		return err
	}
	digest, err := digestReader(input, maxStampBytes)
	if err != nil {
		return err
	}
	stamp := makeStamp(digest, key)
	enc := json.NewEncoder(output)
	enc.SetEscapeHTML(false)
	return enc.Encode(stamp)
}

func runVerify(args []string, input io.Reader, output io.Writer) error {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	stampFile := fs.String("stamp", "", "secure-stamp JSON file")
	publicKeyFile := fs.String("public-key", "", "optional trusted Ed25519 public-key PEM file")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 || *stampFile == "" {
		return errors.New("verify: --stamp is required and positional arguments are not accepted")
	}
	if !filepath.IsAbs(*stampFile) {
		return errors.New("verify: --stamp must be an absolute path")
	}
	if *publicKeyFile != "" && !filepath.IsAbs(*publicKeyFile) {
		return errors.New("verify: --public-key must be an absolute path")
	}
	stamp, err := loadStamp(*stampFile)
	if err != nil {
		return err
	}
	digest, err := digestReader(input, maxStampBytes)
	if err != nil {
		return err
	}
	var trusted ed25519.PublicKey
	if *publicKeyFile != "" {
		trusted, err = loadPublicKey(*publicKeyFile)
		if err != nil {
			return err
		}
	}
	valid, err := verifyStamp(digest, stamp, trusted)
	if err != nil {
		return err
	}
	result := verifyResult{
		Schema:        verifySchema,
		Valid:         valid,
		PayloadSHA256: stamp.PayloadSHA256,
		PublicKeyHex:  stamp.PublicKeyHex,
	}
	enc := json.NewEncoder(output)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(result); err != nil {
		return err
	}
	if !valid {
		return errors.New("verify: stamp is not valid for the supplied bytes/key policy")
	}
	return nil
}

func runGenKey(args []string) error {
	fs := flag.NewFlagSet("gen-key", flag.ContinueOnError)
	privateFile := fs.String("private", "", "output PKCS#8 PEM private-key path")
	publicFile := fs.String("public", "", "output PKIX PEM public-key path")
	force := fs.Bool("force", false, "replace existing files")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 || *privateFile == "" || *publicFile == "" {
		return errors.New("gen-key: --private and --public are required")
	}
	if !filepath.IsAbs(*privateFile) || !filepath.IsAbs(*publicFile) {
		return errors.New("gen-key: --private and --public must be absolute paths")
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return fmt.Errorf("gen-key: %w", err)
	}
	privDER, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return fmt.Errorf("gen-key: marshal private key: %w", err)
	}
	pubDER, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return fmt.Errorf("gen-key: marshal public key: %w", err)
	}
	if err := writePEMFile(*privateFile, "PRIVATE KEY", privDER, 0o600, *force); err != nil {
		return err
	}
	if err := writePEMFile(*publicFile, "PUBLIC KEY", pubDER, 0o644, *force); err != nil {
		return err
	}
	return nil
}

func digestReader(r io.Reader, limit int64) ([sha256.Size]byte, error) {
	var zero [sha256.Size]byte
	if limit <= 0 {
		return zero, errors.New("digest: invalid byte limit")
	}
	h := sha256.New()
	n, err := io.Copy(h, io.LimitReader(r, limit+1))
	if err != nil {
		return zero, fmt.Errorf("digest: read input: %w", err)
	}
	if n > limit {
		return zero, fmt.Errorf("digest: input exceeds %d-byte limit", limit)
	}
	var digest [sha256.Size]byte
	copy(digest[:], h.Sum(nil))
	return digest, nil
}

func makeStamp(digest [sha256.Size]byte, key ed25519.PrivateKey) secureStamp {
	publicKey := key.Public().(ed25519.PublicKey)
	signature := ed25519.Sign(key, stampMessage(digest))
	return secureStamp{
		Schema:        stampSchema,
		Algorithm:     stampAlgorithm,
		PayloadSHA256: hex.EncodeToString(digest[:]),
		PublicKeyHex:  hex.EncodeToString(publicKey),
		SignatureHex:  hex.EncodeToString(signature),
	}
}

func verifyStamp(digest [sha256.Size]byte, stamp secureStamp, trusted ed25519.PublicKey) (bool, error) {
	if stamp.Schema != stampSchema || stamp.Algorithm != stampAlgorithm {
		return false, errors.New("verify: unsupported stamp schema or algorithm")
	}
	if !isCanonicalLowerHex(stamp.PayloadSHA256, sha256.Size) ||
		!isCanonicalLowerHex(stamp.PublicKeyHex, ed25519.PublicKeySize) ||
		!isCanonicalLowerHex(stamp.SignatureHex, ed25519.SignatureSize) {
		return false, errors.New("verify: noncanonical stamp hex encoding")
	}
	claimedDigest, _ := hex.DecodeString(stamp.PayloadSHA256)
	publicKeyBytes, _ := hex.DecodeString(stamp.PublicKeyHex)
	signature, _ := hex.DecodeString(stamp.SignatureHex)
	if subtle.ConstantTimeCompare(claimedDigest, digest[:]) != 1 {
		return false, nil
	}
	publicKey := ed25519.PublicKey(publicKeyBytes)
	if trusted != nil && subtle.ConstantTimeCompare(publicKey, trusted) != 1 {
		return false, nil
	}
	return ed25519.Verify(publicKey, stampMessage(digest), signature), nil
}

func stampMessage(digest [sha256.Size]byte) []byte {
	message := make([]byte, 0, len(stampDomain)+len(digest))
	message = append(message, stampDomain...)
	message = append(message, digest[:]...)
	return message
}

func isCanonicalLowerHex(s string, decodedBytes int) bool {
	if len(s) != decodedBytes*2 || s != strings.ToLower(s) {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

func loadStamp(path string) (secureStamp, error) {
	var stamp secureStamp
	f, err := os.Open(path)
	if err != nil {
		return stamp, fmt.Errorf("verify: open stamp: %w", err)
	}
	defer f.Close()
	dec := json.NewDecoder(io.LimitReader(f, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&stamp); err != nil {
		return stamp, fmt.Errorf("verify: decode stamp: %w", err)
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return stamp, errors.New("verify: trailing JSON after stamp")
		}
		return stamp, fmt.Errorf("verify: trailing stamp data: %w", err)
	}
	return stamp, nil
}

func loadPrivateKey(path string) (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("private key: %w", err)
	}
	block, rest := pem.Decode(data)
	if block == nil || len(strings.TrimSpace(string(rest))) != 0 {
		return nil, errors.New("private key: expected one PEM block")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("private key: parse PKCS#8: %w", err)
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok || len(key) != ed25519.PrivateKeySize {
		return nil, errors.New("private key: expected Ed25519 PKCS#8 key")
	}
	return key, nil
}

func loadPublicKey(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("public key: %w", err)
	}
	block, rest := pem.Decode(data)
	if block == nil || len(strings.TrimSpace(string(rest))) != 0 {
		return nil, errors.New("public key: expected one PEM block")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("public key: parse PKIX: %w", err)
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok || len(key) != ed25519.PublicKeySize {
		return nil, errors.New("public key: expected Ed25519 key")
	}
	return key, nil
}

func writePEMFile(path, typ string, der []byte, mode os.FileMode, force bool) error {
	flags := os.O_WRONLY | os.O_CREATE
	if force {
		flags |= os.O_TRUNC
	} else {
		flags |= os.O_EXCL
	}
	f, err := os.OpenFile(path, flags, mode)
	if err != nil {
		return fmt.Errorf("write key %s: %w", path, err)
	}
	ok := false
	defer func() {
		_ = f.Close()
		if !ok {
			_ = os.Remove(path)
		}
	}()
	// os.OpenFile's mode applies only when a file is created. A forced replacement
	// may already exist with looser permissions, so reassert the requested mode.
	if err := f.Chmod(mode); err != nil {
		return fmt.Errorf("chmod key %s: %w", path, err)
	}
	if err := pem.Encode(f, &pem.Block{Type: typ, Bytes: der}); err != nil {
		return fmt.Errorf("write key %s: %w", path, err)
	}
	if err := f.Sync(); err != nil {
		return fmt.Errorf("sync key %s: %w", path, err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close key %s: %w", path, err)
	}
	ok = true
	return nil
}
