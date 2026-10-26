// Rule test for src/lemoncrow/pro/capabilities/review/security_rules/go.yaml.
// Run it with `opengrep scan --test --config <rules dir> tests/fixtures/opengrep`. Nothing here runs: it is sample
// text for the scanner.
package sample

import (
	"crypto/aes"
	"crypto/des"
	"crypto/md5"
	"crypto/sha256"
	"crypto/tls"
	"database/sql"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
)

func shell(name string) {
	// ruleid: lemoncrow.go.exec-shell-dynamic
	exec.Command("sh", "-c", "ls "+name).Run()
	// ok: lemoncrow.go.exec-shell-dynamic
	exec.Command("sh", "-c", "ls -l").Run()
	// ok: lemoncrow.go.exec-shell-dynamic
	exec.Command("ls", name).Run()
}

func query(db *sql.DB, id string, name string) {
	// ruleid: lemoncrow.go.sql-string-building
	db.Query(fmt.Sprintf("SELECT * FROM users WHERE id = %s", id))
	// ruleid: lemoncrow.go.sql-string-building
	db.Exec("DELETE FROM users WHERE name = '" + name + "'")
	// ok: lemoncrow.go.sql-string-building
	db.Query("SELECT * FROM users WHERE id = ?", id)
}

func crypto(key []byte, data []byte) {
	// ruleid: lemoncrow.go.weak-hash
	md5.Sum(data)
	// ok: lemoncrow.go.weak-hash
	sha256.Sum256(data)
	// ruleid: lemoncrow.go.weak-cipher
	des.NewCipher(key)
	// ok: lemoncrow.go.weak-cipher
	aes.NewCipher(key)
}

func tlsConfig() *tls.Config {
	// ruleid: lemoncrow.go.tls-verification-disabled
	bad := &tls.Config{InsecureSkipVerify: true}
	_ = bad
	// ok: lemoncrow.go.tls-verification-disabled
	return &tls.Config{MinVersion: tls.VersionTLS12}
}

func proxy(w http.ResponseWriter, r *http.Request) {
	target := r.FormValue("url")
	// ruleid: lemoncrow.go.ssrf-user-controlled-url
	http.Get(target)
	// ok: lemoncrow.go.ssrf-user-controlled-url
	http.Get("https://api.example.invalid/status")
}

func serve(w http.ResponseWriter, r *http.Request) {
	name := r.URL.Query().Get("file")
	// ruleid: lemoncrow.go.path-traversal-user-input
	os.Open(filepath.Join("/srv/files", name))
	// ok: lemoncrow.go.path-traversal-user-input
	os.Open(filepath.Join("/srv/files", filepath.Base(name)))
	// ok: lemoncrow.go.path-traversal-user-input
	os.Open("/srv/files/fixed.txt")
}
