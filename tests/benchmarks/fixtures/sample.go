// Sample Go file used as an A/B fixture for the generic outline fallback.
// Patterned after real Go services — not copied from any specific project.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

// Config drives runtime behavior. All fields have safe zero-values.
type Config struct {
	ListenAddr   string
	ReadTimeout  time.Duration
	WriteTimeout time.Duration
	IdleTimeout  time.Duration
	MaxBytes     int64
	ShutdownGrace time.Duration
}

// Server wraps the http.Server with structured logging and graceful shutdown.
type Server struct {
	cfg     Config
	http    *http.Server
	logger  *slog.Logger
	mu      sync.RWMutex
	started bool
	routes  map[string]http.Handler
}

// NewServer constructs a Server with sensible defaults applied to cfg.
func NewServer(cfg Config, logger *slog.Logger) *Server {
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = ":8080"
	}
	if cfg.ReadTimeout == 0 {
		cfg.ReadTimeout = 5 * time.Second
	}
	if cfg.WriteTimeout == 0 {
		cfg.WriteTimeout = 10 * time.Second
	}
	if cfg.IdleTimeout == 0 {
		cfg.IdleTimeout = 60 * time.Second
	}
	if cfg.MaxBytes == 0 {
		cfg.MaxBytes = 1 << 20
	}
	if cfg.ShutdownGrace == 0 {
		cfg.ShutdownGrace = 10 * time.Second
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &Server{
		cfg:    cfg,
		logger: logger,
		routes: make(map[string]http.Handler),
	}
}

// Handle registers an http.Handler for the given path. Panics if the path is empty.
func (s *Server) Handle(path string, h http.Handler) {
	if path == "" {
		panic("server.Handle: empty path")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.routes[path] = h
}

// HandleFunc is a convenience wrapper around Handle for func handlers.
func (s *Server) HandleFunc(path string, fn func(http.ResponseWriter, *http.Request)) {
	s.Handle(path, http.HandlerFunc(fn))
}

// Start binds to ListenAddr and serves until Stop is called or the listener errors.
func (s *Server) Start(ctx context.Context) error {
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return errors.New("server already started")
	}
	mux := http.NewServeMux()
	for path, h := range s.routes {
		mux.Handle(path, s.wrap(h))
	}
	s.http = &http.Server{
		Addr:         s.cfg.ListenAddr,
		Handler:      mux,
		ReadTimeout:  s.cfg.ReadTimeout,
		WriteTimeout: s.cfg.WriteTimeout,
		IdleTimeout:  s.cfg.IdleTimeout,
		BaseContext:  func(_ net.Listener) context.Context { return ctx },
	}
	s.started = true
	s.mu.Unlock()
	s.logger.Info("server starting", "addr", s.cfg.ListenAddr)
	return s.http.ListenAndServe()
}

// Stop drains in-flight requests up to ShutdownGrace, then forces closure.
func (s *Server) Stop(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.started {
		return nil
	}
	shutdownCtx, cancel := context.WithTimeout(ctx, s.cfg.ShutdownGrace)
	defer cancel()
	s.logger.Info("server stopping", "grace", s.cfg.ShutdownGrace)
	if err := s.http.Shutdown(shutdownCtx); err != nil {
		s.logger.Warn("shutdown error, forcing close", "err", err)
		return s.http.Close()
	}
	s.started = false
	return nil
}

func (s *Server) wrap(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		r.Body = http.MaxBytesReader(w, r.Body, s.cfg.MaxBytes)
		defer func() {
			if rec := recover(); rec != nil {
				s.logger.Error("handler panic", "path", r.URL.Path, "recover", rec)
				http.Error(w, "internal error", http.StatusInternalServerError)
			}
			s.logger.Info("request",
				"method", r.Method,
				"path", r.URL.Path,
				"elapsed", time.Since(start),
			)
		}()
		h.ServeHTTP(w, r)
	})
}

// WriteJSON is a small helper that handles encoding errors uniformly.
func WriteJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		fmt.Fprintln(w, `{"error":"encode failed"}`)
	}
}
