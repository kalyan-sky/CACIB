// txn-service is the single binary behind every simulated banking application.
//
// Each of the 200+ apps in the inventory runs this same image with different
// configuration, the way a platform team often sees many near-identical
// Spring Boot / Go microservices. It has three modes:
//
//	txn-service                 serve: an HTTP "banking" microservice (default)
//	txn-service loadgen         traffic simulator that drives the edge apps
//	txn-service portal          corporate e-banking web portal (UI + BFF API)
//	txn-service probe <url>     tiny HTTP client for scratch images (exit 0/1)
//
// Behaviour is controlled entirely through environment variables so that
// incidents can be injected with nothing more than kubectl.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

var version = "1.5.0"

func main() {
	log.SetFlags(0)
	log.SetOutput(os.Stdout)

	mode := "serve"
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	switch mode {
	case "serve":
		serve()
	case "loadgen":
		loadgen()
	case "portal":
		servePortal()
	case "probe":
		os.Exit(probe(os.Args[2:]))
	case "version":
		fmt.Println(version)
	default:
		fmt.Fprintf(os.Stderr, "unknown mode %q (serve|loadgen|portal|probe|version)\n", mode)
		os.Exit(2)
	}
}

// ---------------------------------------------------------------- helpers

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	v, err := strconv.Atoi(env(key, ""))
	if err != nil {
		return def
	}
	return v
}

func envFloat(key string, def float64) float64 {
	v, err := strconv.ParseFloat(env(key, ""), 64)
	if err != nil {
		return def
	}
	return v
}

func splitList(s string) []string {
	var out []string
	for _, p := range strings.Split(s, ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// logJSON writes one structured log line, as a bank's log pipeline expects.
func logJSON(level, msg string, fields map[string]any) {
	rec := map[string]any{
		"ts":    time.Now().UTC().Format(time.RFC3339Nano),
		"level": level,
		"app":   env("APP_NAME", "txn-service"),
		"msg":   msg,
	}
	for k, v := range fields {
		rec[k] = v
	}
	b, _ := json.Marshal(rec)
	log.Println(string(b))
}

// ---------------------------------------------------------------- serve

type metrics struct {
	requests  atomic.Int64
	failures  atomic.Int64
	upErrors  sync.Map // upstream -> *atomic.Int64
	latencyMs atomic.Int64
}

func (m *metrics) upstreamErr(name string) {
	v, _ := m.upErrors.LoadOrStore(name, new(atomic.Int64))
	v.(*atomic.Int64).Add(1)
}

func serve() {
	app := env("APP_NAME", "txn-service")
	domain := env("DOMAIN", "unknown")
	port := env("PORT", "8080")

	// Fail fast on missing mandatory configuration, like a real service
	// whose datasource URL is absent. This is what CrashLoopBackOff looks like.
	for _, key := range splitList(env("REQUIRED_CONFIG", "")) {
		if os.Getenv(key) == "" {
			logJSON("FATAL", "missing required configuration, refusing to start",
				map[string]any{"key": key, "hint": "check the ConfigMap referenced by envFrom"})
			os.Exit(1)
		}
	}

	upstreams := splitList(env("UPSTREAMS", ""))
	failRate := envFloat("FAIL_RATE", 0)
	latency := envInt("LATENCY_MS", 5)
	startupDelay := envInt("STARTUP_DELAY_MS", 500)
	leakMBps := envInt("LEAK_MB_PER_SEC", 0)

	m := &metrics{}
	var ready atomic.Bool
	go func() {
		time.Sleep(time.Duration(startupDelay) * time.Millisecond)
		ready.Store(true)
	}()

	if leakMBps > 0 {
		go leak(leakMBps)
	}

	client := &http.Client{Timeout: time.Duration(envInt("UPSTREAM_TIMEOUT_MS", 2000)) * time.Millisecond}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if !ready.Load() {
			http.Error(w, "starting", http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ready\n"))
	})
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		labels := fmt.Sprintf(`app=%q,domain=%q`, app, domain)
		fmt.Fprintf(w, "# TYPE txn_requests_total counter\ntxn_requests_total{%s} %d\n", labels, m.requests.Load())
		fmt.Fprintf(w, "# TYPE txn_failures_total counter\ntxn_failures_total{%s} %d\n", labels, m.failures.Load())
		fmt.Fprintf(w, "# TYPE txn_latency_ms_sum counter\ntxn_latency_ms_sum{%s} %d\n", labels, m.latencyMs.Load())
		fmt.Fprintf(w, "# TYPE txn_upstream_errors_total counter\n")
		m.upErrors.Range(func(k, v any) bool {
			fmt.Fprintf(w, "txn_upstream_errors_total{%s,upstream=%q} %d\n", labels, k, v.(*atomic.Int64).Load())
			return true
		})
		fmt.Fprintf(w, "# TYPE txn_build_info gauge\ntxn_build_info{%s,version=%q} 1\n", labels, version)
	})
	mux.HandleFunc("/transaction", func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		m.requests.Add(1)
		txnID := r.Header.Get("X-Txn-Id")
		if txnID == "" {
			txnID = fmt.Sprintf("TXN-%d", rand.Int63())
		}
		time.Sleep(time.Duration(latency+rand.Intn(latency+1)) * time.Millisecond)

		path := []string{app}
		status := http.StatusOK
		errMsg := ""
		for _, up := range upstreams {
			req, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, up+"/transaction", nil)
			req.Header.Set("X-Txn-Id", txnID)
			resp, err := client.Do(req)
			if err != nil {
				status, errMsg = http.StatusBadGateway, fmt.Sprintf("upstream %s: %v", up, err)
				m.upstreamErr(up)
				break
			}
			var body struct {
				Path []string `json:"path"`
			}
			json.NewDecoder(resp.Body).Decode(&body)
			resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				status, errMsg = http.StatusBadGateway, fmt.Sprintf("upstream %s returned %d", up, resp.StatusCode)
				m.upstreamErr(up)
				break
			}
			path = append(path, body.Path...)
		}
		if status == http.StatusOK && rand.Float64() < failRate {
			status, errMsg = http.StatusInternalServerError, "simulated application error"
		}

		elapsed := time.Since(start).Milliseconds()
		m.latencyMs.Add(elapsed)
		if status != http.StatusOK {
			m.failures.Add(1)
			logJSON("ERROR", "transaction failed", map[string]any{"txn_id": txnID, "error": errMsg, "latency_ms": elapsed})
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]any{
			"app": app, "domain": domain, "txn_id": txnID, "status": status,
			"error": errMsg, "path": path, "latency_ms": elapsed, "version": version,
		})
	})

	srv := &http.Server{Addr: ":" + port, Handler: mux}
	go func() {
		logJSON("INFO", "service started", map[string]any{
			"port": port, "domain": domain, "version": version, "upstreams": upstreams,
		})
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logJSON("FATAL", "listen failed", map[string]any{"error": err.Error()})
			os.Exit(1)
		}
	}()
	waitForShutdown(srv)
}

// leak simulates a release with an unbounded in-memory cache: memory grows
// until the container's limit is hit and the kernel OOM-kills it.
func leak(mbPerSec int) {
	logJSON("WARN", "cache warm-up enabled", map[string]any{"mb_per_sec": mbPerSec})
	var hoard [][]byte
	for range time.Tick(time.Second) {
		chunk := make([]byte, mbPerSec<<20)
		for i := range chunk {
			chunk[i] = byte(i) // touch every page so it counts towards RSS
		}
		hoard = append(hoard, chunk)
		logJSON("INFO", "cache size", map[string]any{"mb": len(hoard) * mbPerSec})
	}
}

func waitForShutdown(srv *http.Server) {
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT)
	<-sig
	logJSON("INFO", "SIGTERM received, draining connections", nil)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
}

// ---------------------------------------------------------------- loadgen

type targetStats struct {
	mu      sync.Mutex
	ok, err int64
	window  []sample // last 60s
	lastErr string
}

type sample struct {
	at  time.Time
	ok  bool
	lat time.Duration
}

func (t *targetStats) record(ok bool, lat time.Duration, errMsg string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	if ok {
		t.ok++
	} else {
		t.err++
		t.lastErr = errMsg
	}
	t.window = append(t.window, sample{now, ok, lat})
	cut := 0
	for cut < len(t.window) && now.Sub(t.window[cut].at) > time.Minute {
		cut++
	}
	t.window = t.window[cut:]
}

type summary struct {
	Target      string  `json:"target"`
	Requests60s int     `json:"requests_60s"`
	SuccessPct  float64 `json:"success_pct_60s"`
	P95Ms       int64   `json:"p95_ms_60s"`
	TotalOK     int64   `json:"total_ok"`
	TotalErr    int64   `json:"total_err"`
	LastError   string  `json:"last_error,omitempty"`
	Status      string  `json:"status"`
}

func (t *targetStats) summarise(name string) summary {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := summary{Target: name, TotalOK: t.ok, TotalErr: t.err, LastError: t.lastErr, Requests60s: len(t.window)}
	var lats []time.Duration
	okCount := 0
	for _, w := range t.window {
		if w.ok {
			okCount++
		}
		lats = append(lats, w.lat)
	}
	if len(t.window) > 0 {
		s.SuccessPct = float64(int(1000*float64(okCount)/float64(len(t.window)))) / 10
		sort.Slice(lats, func(i, j int) bool { return lats[i] < lats[j] })
		s.P95Ms = lats[len(lats)*95/100].Milliseconds()
	}
	switch {
	case len(t.window) == 0:
		s.Status = "NO DATA"
	case s.SuccessPct >= 99:
		s.Status = "HEALTHY"
	case s.SuccessPct >= 90:
		s.Status = "DEGRADED"
	default:
		s.Status = "DOWN"
	}
	return s
}

func loadgen() {
	targets := splitList(env("TARGETS", ""))
	rps := max(envInt("RPS", 2), 1)
	if len(targets) == 0 {
		logJSON("FATAL", "TARGETS is empty", nil)
		os.Exit(1)
	}
	stats := map[string]*targetStats{}
	for _, t := range targets {
		stats[t] = &targetStats{}
	}
	client := &http.Client{Timeout: 5 * time.Second}

	for _, t := range targets {
		go func(target string) {
			for range time.Tick(time.Second / time.Duration(rps)) {
				go func() {
					start := time.Now()
					resp, err := client.Get(target + "/transaction")
					lat := time.Since(start)
					if err != nil {
						stats[target].record(false, lat, err.Error())
						return
					}
					var body struct {
						Error string `json:"error"`
					}
					json.NewDecoder(resp.Body).Decode(&body)
					resp.Body.Close()
					stats[target].record(resp.StatusCode == 200, lat, fmt.Sprintf("%d %s", resp.StatusCode, body.Error))
				}()
			}
		}(t)
	}

	all := func() []summary {
		var out []summary
		for _, t := range targets {
			out = append(out, stats[t].summarise(t))
		}
		return out
	}

	go func() {
		for range time.Tick(15 * time.Second) {
			for _, s := range all() {
				logJSON("INFO", "slo", map[string]any{
					"target": s.Target, "status": s.Status, "success_pct_60s": s.SuccessPct,
					"p95_ms": s.P95Ms, "last_error": s.LastError,
				})
			}
		}
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ready\n")) })
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(all())
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, dashboardHead)
		for _, s := range all() {
			fmt.Fprintf(w, `<tr class="%s"><td>%s</td><td>%s</td><td>%.1f%%</td><td>%d ms</td><td>%d</td><td>%s</td></tr>`,
				strings.ToLower(strings.ReplaceAll(s.Status, " ", "")), s.Target, s.Status, s.SuccessPct, s.P95Ms, s.Requests60s, htmlEscape(s.LastError))
		}
		fmt.Fprint(w, dashboardTail)
	})
	srv := &http.Server{Addr: ":" + env("PORT", "8080"), Handler: mux}
	go func() {
		logJSON("INFO", "traffic simulator started", map[string]any{"targets": targets, "rps_per_target": rps})
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			os.Exit(1)
		}
	}()
	waitForShutdown(srv)
}

func htmlEscape(s string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(s)
}

const dashboardHead = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>Bank Sim - Service Health</title><style>
body{font-family:system-ui,sans-serif;margin:2rem;background:#0f172a;color:#e2e8f0}
table{border-collapse:collapse;width:100%}td,th{padding:.5rem .75rem;border-bottom:1px solid #334155;text-align:left}
.healthy td:nth-child(2){color:#4ade80}.degraded td:nth-child(2){color:#facc15}.down td:nth-child(2),.nodata td:nth-child(2){color:#f87171}
td:last-child{font-family:monospace;font-size:.8rem;color:#94a3b8}</style></head><body>
<h1>Customer-facing transaction flows</h1><p>Synthetic traffic, 60 s window, auto-refresh 5 s.</p>
<table><tr><th>Entry point</th><th>Status</th><th>Success</th><th>p95</th><th>Requests</th><th>Last error</th></tr>`

const dashboardTail = `</table></body></html>`

// ---------------------------------------------------------------- probe

// probe is a minimal curl replacement so engineers can test connectivity
// from inside scratch-based containers: kubectl exec ... -- /txn-service probe URL
func probe(args []string) int {
	if len(args) != 1 {
		fmt.Fprintln(os.Stderr, "usage: txn-service probe <url>")
		return 2
	}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(args[0])
	if err != nil {
		fmt.Println("FAIL:", err)
		return 1
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	fmt.Println(resp.Status)
	fmt.Println(strings.TrimSpace(string(body)))
	if resp.StatusCode >= 400 {
		return 1
	}
	return 0
}
