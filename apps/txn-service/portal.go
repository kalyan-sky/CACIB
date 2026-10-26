package main

// portal mode: the corporate e-banking web portal of the simulated bank.
//
// It serves the single-page UI in web/ and a small backend-for-frontend (BFF)
// API. The BFF keeps a demo client's accounts in memory, but every customer
// action is processed by the real edge services in the cluster (payments,
// PayNow, letters of credit, KYC, account sync). When an incident scenario
// breaks one of them, the portal shows the customer-facing impact, together
// with the trace of services that handled (or failed) the request.
//
// Banking conventions followed on purpose: money is integer minor units
// (cents), payments carry an idempotency key, large payments need a second
// person to approve (maker-checker), and inputs are validated strictly.

import (
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed web
var webFS embed.FS

const (
	approvalThresholdSGD = 50_000_00  // minor units: payments above S$50,000 need a checker
	fastLimitSGD         = 200_000_00 // FAST / PayNow per-transaction limit
	maxAmountMinor       = 99_999_999_99
	maxListed            = 50
)

// Demo FX rates to SGD, scaled by 10,000 (1 USD = 1.3500 SGD).
var fxToSGD = map[string]int64{"SGD": 10000, "USD": 13500, "EUR": 14600}

type demoUser struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Role string `json:"role"` // maker | checker
}

var demoUsers = []demoUser{
	{"alex", "Alex Tan", "maker"},
	{"priya", "Priya Nair", "checker"},
}

// backend is one edge service the portal depends on.
type backend struct {
	Key     string `json:"key"`
	Label   string `json:"label"`
	UsedFor string `json:"used_for"`
	URL     string `json:"-"`
}

type Account struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Number   string `json:"number"`
	Currency string `json:"currency"`
	Balance  int64  `json:"balance_minor"`
}

type Beneficiary struct {
	ID       string   `json:"id"`
	Name     string   `json:"name"`
	Bank     string   `json:"bank"`
	Account  string   `json:"account"`
	Country  string   `json:"country"`
	Currency string   `json:"currency"`
	Rails    []string `json:"rails"`
}

type Trace struct {
	Service    string    `json:"service"`
	OK         bool      `json:"ok"`
	StatusCode int       `json:"status_code"`
	LatencyMs  int64     `json:"latency_ms"`
	Path       []string  `json:"path"`
	Error      string    `json:"error,omitempty"`
	At         time.Time `json:"at"`
}

type Txn struct {
	ID          string    `json:"id"`
	AccountID   string    `json:"account_id"`
	AccountName string    `json:"account_name"`
	Currency    string    `json:"currency"`
	At          time.Time `json:"at"`
	Text        string    `json:"description"`
	Amount      int64     `json:"amount_minor"` // signed, account currency
	Balance     int64     `json:"balance_after_minor"`
	Ref         string    `json:"reference"`
}

type Payment struct {
	ID            string    `json:"id"`
	FromAccount   string    `json:"from_account"`
	BeneficiaryID string    `json:"beneficiary_id"`
	Beneficiary   string    `json:"beneficiary"`
	Rail          string    `json:"rail"`
	Amount        int64     `json:"amount_minor"`
	Currency      string    `json:"currency"`
	DebitAmount   int64     `json:"debit_minor"`
	DebitCurrency string    `json:"debit_currency"`
	FXRate        string    `json:"fx_rate,omitempty"`
	Reference     string    `json:"reference"`
	Status        string    `json:"status"` // PROCESSING, PENDING_APPROVAL, COMPLETED, FAILED, REJECTED
	Error         string    `json:"error,omitempty"`
	CreatedBy     string    `json:"created_by"`
	ApprovedBy    string    `json:"approved_by,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
	Trace         *Trace    `json:"trace,omitempty"`
}

type LetterOfCredit struct {
	ID          string    `json:"id"`
	Applicant   string    `json:"applicant_account"`
	Beneficiary string    `json:"beneficiary"`
	Country     string    `json:"country"`
	Amount      int64     `json:"amount_minor"`
	Currency    string    `json:"currency"`
	Expiry      string    `json:"expiry"`
	Goods       string    `json:"goods"`
	Status      string    `json:"status"` // ISSUED, FAILED
	Error       string    `json:"error,omitempty"`
	CreatedBy   string    `json:"created_by"`
	CreatedAt   time.Time `json:"created_at"`
	Trace       *Trace    `json:"trace,omitempty"`
}

type KYCCase struct {
	ID        string    `json:"id"`
	Kind      string    `json:"kind"` // new-beneficiary | new-subsidiary | periodic-review
	Entity    string    `json:"entity"`
	Country   string    `json:"country"`
	Status    string    `json:"status"` // SUBMITTED, FAILED
	Error     string    `json:"error,omitempty"`
	CreatedBy string    `json:"created_by"`
	CreatedAt time.Time `json:"created_at"`
	Trace     *Trace    `json:"trace,omitempty"`
}

type callStats struct {
	recent []Trace // last 50 calls
}

type portal struct {
	company  string
	backends []backend
	byKey    map[string]backend
	client   *http.Client
	mu       sync.Mutex
	accounts []*Account
	benes    []Beneficiary
	txns     []Txn
	payments []*Payment
	lcs      []*LetterOfCredit
	kyc      []*KYCCase
	idem     map[string]string // idempotency key -> payment id
	seq      int
	stats    map[string]*callStats
	now      func() time.Time
}

func newPortal(urls map[string]string) *portal {
	backends := []backend{
		{"accounts", "Account services", "Balances and statements (corporate-portal-api)", urls["accounts"]},
		{"payments", "Payments gateway", "MEPS+ and SWIFT payments (payments-gateway)", urls["payments"]},
		{"paynow", "FAST / PayNow", "Real-time SGD payments (paynow-fast-adapter)", urls["paynow"]},
		{"trade", "Trade finance", "Letters of credit (lc-issuance)", urls["trade"]},
		{"kyc", "Client onboarding", "KYC and screening (kyc-onboarding)", urls["kyc"]},
	}
	p := &portal{
		company:  "Demo Logistics Pte Ltd",
		backends: backends,
		byKey:    map[string]backend{},
		client:   &http.Client{Timeout: 8 * time.Second},
		idem:     map[string]string{},
		seq:      1000,
		stats:    map[string]*callStats{},
		now:      time.Now,
	}
	for _, b := range backends {
		p.byKey[b.Key] = b
		p.stats[b.Key] = &callStats{}
	}
	p.seed()
	return p
}

func (p *portal) seed() {
	p.accounts = []*Account{
		{"sgd-ops", "Operating Account", "001-482913-001", "SGD", 2_450_000_00},
		{"sgd-pay", "Payroll Account", "001-482913-002", "SGD", 380_000_00},
		{"usd-trade", "USD Trade Account", "001-482913-501", "USD", 830_500_00},
		{"eur-coll", "EUR Collections", "001-482913-701", "EUR", 215_300_00},
	}
	p.benes = []Beneficiary{
		{"pacific-freight", "Pacific Freight Services Pte Ltd", "DEMO Bank Singapore", "UEN-DEMO-0001", "SG", "SGD", []string{"PAYNOW", "MEPS"}},
		{"changi-warehousing", "Changi Warehousing Pte Ltd", "Sample Bank SG", "123-456789-0", "SG", "SGD", []string{"PAYNOW", "MEPS"}},
		{"shenzhen-components", "Shenzhen Components Co Ltd", "Example Bank Shenzhen (DEMOCNSZXXX)", "6222-0000-1234", "CN", "USD", []string{"SWIFT"}},
		{"rotterdam-port", "Rotterdam Port Supplies BV", "Voorbeeld Bank (DEMONL2AXXX)", "NL00DEMO0123456789", "NL", "EUR", []string{"SWIFT"}},
	}
	base := p.now().Add(-72 * time.Hour)
	hist := []struct {
		acct, text, ref string
		amount          int64
	}{
		{"sgd-ops", "Customer receipt - Harbour Retail", "INV-2026-0912", 125_400_00},
		{"sgd-ops", "PayNow to Pacific Freight Services", "PO-7781", -18_250_00},
		{"usd-trade", "SWIFT to Shenzhen Components Co", "PO-7765", -42_000_00},
		{"sgd-pay", "Salary batch October", "PAYROLL-10", -96_500_00},
		{"eur-coll", "Incoming SEPA - Nordic Cargo AB", "INV-2026-0899", 18_900_00},
	}
	for i, h := range hist {
		a := p.account(h.acct)
		p.txns = append(p.txns, Txn{
			ID: fmt.Sprintf("TXN-%06d", 900+i), AccountID: a.ID, AccountName: a.Name, Currency: a.Currency, At: base.Add(time.Duration(i) * 9 * time.Hour),
			Text: h.text, Amount: h.amount, Balance: a.Balance, Ref: h.ref,
		})
	}
}

func (p *portal) account(id string) *Account {
	for _, a := range p.accounts {
		if a.ID == id {
			return a
		}
	}
	return nil
}

func (p *portal) beneficiary(id string) *Beneficiary {
	for i := range p.benes {
		if p.benes[i].ID == id {
			return &p.benes[i]
		}
	}
	return nil
}

func (p *portal) nextID(prefix string) string {
	p.seq++
	return fmt.Sprintf("%s-%06d", prefix, p.seq)
}

// ---------------------------------------------------------------- backend calls

// call sends one transaction to an edge service and records the outcome.
func (p *portal) call(key, txnID string) *Trace {
	b := p.byKey[key]
	t := &Trace{Service: key, At: p.now()}
	if b.URL == "" {
		t.Error = "service not configured"
		p.record(key, *t)
		return t
	}
	start := time.Now()
	req, _ := http.NewRequest(http.MethodGet, b.URL+"/transaction", nil)
	req.Header.Set("X-Txn-Id", txnID)
	resp, err := p.client.Do(req)
	t.LatencyMs = time.Since(start).Milliseconds()
	if err != nil {
		t.Error = cleanErr(err)
		p.record(key, *t)
		return t
	}
	defer resp.Body.Close()
	var body struct {
		Path  []string `json:"path"`
		Error string   `json:"error"`
	}
	json.NewDecoder(http.MaxBytesReader(nil, resp.Body, 1<<16)).Decode(&body)
	t.StatusCode, t.Path, t.Error = resp.StatusCode, body.Path, body.Error
	t.OK = resp.StatusCode == http.StatusOK
	if !t.OK && t.Error == "" {
		t.Error = fmt.Sprintf("HTTP %d", resp.StatusCode)
	}
	p.record(key, *t)
	return t
}

// Customer-facing messages: internal service names and addresses stay in the
// trace (for operations staff) and never reach the customer's screen.
const (
	msgPaymentFailed = "We could not process this payment. No money has been debited. Please try again later."
	msgLCFailed      = "We could not submit this application right now. Please try again later."
	msgKYCFailed     = "We could not submit this request right now. Please try again later."
)

// cleanErr turns Go client errors into something an operator can read.
func cleanErr(err error) string {
	s := err.Error()
	switch {
	case strings.Contains(s, "Client.Timeout") || strings.Contains(s, "deadline exceeded"):
		return "timeout waiting for service"
	case strings.Contains(s, "connection refused"):
		return "connection refused (no ready pods behind the service?)"
	case strings.Contains(s, "no such host"):
		return "service DNS name not found"
	}
	return s
}

func (p *portal) record(key string, t Trace) {
	p.mu.Lock()
	defer p.mu.Unlock()
	s := p.stats[key]
	s.recent = append(s.recent, t)
	if len(s.recent) > 50 {
		s.recent = s.recent[len(s.recent)-50:]
	}
}

// ---------------------------------------------------------------- validation

var (
	amountRe = regexp.MustCompile(`^\d{1,11}(\.\d{1,2})?$`)
	textRe   = regexp.MustCompile(`^[\p{L}\p{N} .,&'()/\-#:]{1,100}$`)
	dateRe   = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)
	isoCtry  = regexp.MustCompile(`^[A-Z]{2}$`)
	idemRe   = regexp.MustCompile(`^[A-Za-z0-9-]{8,64}$`)
)

// parseAmount converts "1234.5" into 123450 minor units without floating point.
func parseAmount(s string) (int64, error) {
	s = strings.ReplaceAll(strings.TrimSpace(s), ",", "")
	if !amountRe.MatchString(s) {
		return 0, errors.New("amount must be a number with at most 2 decimals")
	}
	whole, frac, _ := strings.Cut(s, ".")
	for len(frac) < 2 {
		frac += "0"
	}
	w, _ := strconv.ParseInt(whole, 10, 64)
	f, _ := strconv.ParseInt(frac, 10, 64)
	v := w*100 + f
	if v <= 0 {
		return 0, errors.New("amount must be greater than zero")
	}
	if v > maxAmountMinor {
		return 0, errors.New("amount too large")
	}
	return v, nil
}

// convert moves an amount between currencies via SGD, rounding half up.
func convert(amount int64, from, to string) int64 {
	if from == to {
		return amount
	}
	num := amount * fxToSGD[from]
	den := fxToSGD[to]
	return (num + den/2) / den
}

func fxRate(from, to string) string {
	if from == to {
		return ""
	}
	return fmt.Sprintf("1 %s = %.4f %s", from, float64(fxToSGD[from])/float64(fxToSGD[to]), to)
}

// ---------------------------------------------------------------- HTTP plumbing

type apiError struct {
	Status  int
	Message string
	Trace   *Trace
}

func (e *apiError) Error() string { return e.Message }

func badRequest(format string, a ...any) error {
	return &apiError{Status: http.StatusBadRequest, Message: fmt.Sprintf(format, a...)}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func decode(r *http.Request, v any) error {
	dec := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 1<<16))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return badRequest("invalid request body: %v", err)
	}
	return nil
}

// handle adapts a handler that returns (status, body, error) and resolves the demo user.
func (p *portal) handle(fn func(u demoUser, r *http.Request) (int, any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		u, ok := userByID(r.Header.Get("X-Demo-User"))
		if !ok {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unknown user - choose a demo user"})
			return
		}
		status, body, err := fn(u, r)
		if err != nil {
			var ae *apiError
			if !errors.As(err, &ae) {
				ae = &apiError{Status: http.StatusInternalServerError, Message: err.Error()}
			}
			writeJSON(w, ae.Status, map[string]any{"error": ae.Message, "trace": ae.Trace})
			return
		}
		writeJSON(w, status, body)
	}
}

func userByID(id string) (demoUser, bool) {
	for _, u := range demoUsers {
		if u.ID == id {
			return u, true
		}
	}
	return demoUser{}, false
}

// securityHeaders applies the headers a bank's web security standard asks for.
func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		if strings.HasPrefix(r.URL.Path, "/api/") {
			h.Set("Cache-Control", "no-store")
		}
		next.ServeHTTP(w, r)
	})
}

func (p *portal) routes() http.Handler {
	mux := http.NewServeMux()
	static, _ := fs.Sub(webFS, "web")
	mux.Handle("GET /", http.FileServerFS(static))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok\n")) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ready\n")) })
	mux.HandleFunc("GET /api/meta", p.meta) // no user needed: drives the login screen

	mux.Handle("GET /api/accounts", p.handle(p.getAccounts))
	mux.Handle("GET /api/transactions", p.handle(p.getTransactions))
	mux.Handle("GET /api/payments", p.handle(p.listPayments))
	mux.Handle("POST /api/payments", p.handle(p.createPayment))
	mux.Handle("POST /api/payments/{id}/approve", p.handle(p.decidePayment(true)))
	mux.Handle("POST /api/payments/{id}/reject", p.handle(p.decidePayment(false)))
	mux.Handle("GET /api/lcs", p.handle(p.listLCs))
	mux.Handle("POST /api/lcs", p.handle(p.createLC))
	mux.Handle("GET /api/kyc", p.handle(p.listKYC))
	mux.Handle("POST /api/kyc", p.handle(p.createKYC))
	mux.Handle("GET /api/status", p.handle(p.status))
	return securityHeaders(mux)
}

// ---------------------------------------------------------------- handlers

func (p *portal) meta(w http.ResponseWriter, r *http.Request) {
	p.mu.Lock()
	benes := append([]Beneficiary(nil), p.benes...)
	p.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{
		"company": p.company, "users": demoUsers, "beneficiaries": benes,
		"approval_threshold_sgd_minor": approvalThresholdSGD, "fast_limit_sgd_minor": fastLimitSGD,
		"fx_to_sgd": fxToSGD, "backends": p.backends, "version": version,
	})
}

// getAccounts refreshes balances through the account services, the way the
// real portal would. If that path is broken, the customer sees it.
func (p *portal) getAccounts(u demoUser, r *http.Request) (int, any, error) {
	t := p.call("accounts", "BAL-"+strconv.FormatInt(p.now().UnixNano(), 36))
	if !t.OK {
		return 0, nil, &apiError{Status: http.StatusServiceUnavailable, Message: "Accounts unavailable", Trace: t}
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]Account, len(p.accounts))
	for i, a := range p.accounts {
		out[i] = *a
	}
	pending := 0
	for _, pm := range p.payments {
		if pm.Status == "PENDING_APPROVAL" {
			pending++
		}
	}
	return http.StatusOK, map[string]any{"accounts": out, "pending_approvals": pending, "trace": t}, nil
}

func (p *portal) getTransactions(u demoUser, r *http.Request) (int, any, error) {
	acct := r.URL.Query().Get("account")
	p.mu.Lock()
	defer p.mu.Unlock()
	var out []Txn
	for i := len(p.txns) - 1; i >= 0 && len(out) < maxListed; i-- {
		if acct == "" || p.txns[i].AccountID == acct {
			out = append(out, p.txns[i])
		}
	}
	return http.StatusOK, map[string]any{"transactions": out}, nil
}

func (p *portal) listPayments(u demoUser, r *http.Request) (int, any, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := []Payment{}
	for i := len(p.payments) - 1; i >= 0 && len(out) < maxListed; i-- {
		out = append(out, *p.payments[i])
	}
	return http.StatusOK, map[string]any{"payments": out}, nil
}

type paymentRequest struct {
	FromAccount    string `json:"from_account"`
	BeneficiaryID  string `json:"beneficiary_id"`
	Rail           string `json:"rail"`
	Amount         string `json:"amount"`
	Currency       string `json:"currency"`
	Reference      string `json:"reference"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (p *portal) createPayment(u demoUser, r *http.Request) (int, any, error) {
	if u.Role != "maker" {
		return 0, nil, &apiError{Status: http.StatusForbidden, Message: "only a maker can initiate payments"}
	}
	var req paymentRequest
	if err := decode(r, &req); err != nil {
		return 0, nil, err
	}
	if !idemRe.MatchString(req.IdempotencyKey) {
		return 0, nil, badRequest("idempotency_key is required (8-64 letters, digits or dashes)")
	}
	amount, err := parseAmount(req.Amount)
	if err != nil {
		return 0, nil, badRequest("%v", err)
	}
	if _, ok := fxToSGD[req.Currency]; !ok {
		return 0, nil, badRequest("unsupported currency %q", req.Currency)
	}
	if !textRe.MatchString(req.Reference) {
		return 0, nil, badRequest("reference is required (letters, digits and basic punctuation, max 100)")
	}

	p.mu.Lock()
	if id, seen := p.idem[req.IdempotencyKey]; seen {
		pm := *p.findPayment(id)
		p.mu.Unlock()
		return http.StatusOK, map[string]any{"payment": pm, "duplicate": true}, nil
	}
	acct, bene := p.account(req.FromAccount), p.beneficiary(req.BeneficiaryID)
	if acct == nil || bene == nil {
		p.mu.Unlock()
		return 0, nil, badRequest("unknown account or beneficiary")
	}
	if !contains(bene.Rails, req.Rail) {
		p.mu.Unlock()
		return 0, nil, badRequest("%s cannot be paid via %s (use %s)", bene.Name, req.Rail, strings.Join(bene.Rails, " or "))
	}
	if (req.Rail == "PAYNOW" || req.Rail == "MEPS") && req.Currency != "SGD" {
		p.mu.Unlock()
		return 0, nil, badRequest("%s payments must be in SGD", req.Rail)
	}
	sgd := convert(amount, req.Currency, "SGD")
	if req.Rail == "PAYNOW" && sgd > fastLimitSGD {
		p.mu.Unlock()
		return 0, nil, badRequest("FAST / PayNow is limited to S$200,000 per payment - use MEPS+")
	}
	debit := convert(amount, req.Currency, acct.Currency)
	if debit > acct.Balance {
		p.mu.Unlock()
		return 0, nil, &apiError{Status: http.StatusUnprocessableEntity, Message: "insufficient funds in " + acct.Name}
	}
	now := p.now()
	pm := &Payment{
		ID: p.nextID("PAY"), FromAccount: acct.ID, BeneficiaryID: bene.ID, Beneficiary: bene.Name,
		Rail: req.Rail, Amount: amount, Currency: req.Currency, DebitAmount: debit, DebitCurrency: acct.Currency,
		FXRate: fxRate(req.Currency, acct.Currency), Reference: req.Reference, Status: "PROCESSING",
		CreatedBy: u.Name, CreatedAt: now, UpdatedAt: now,
	}
	if sgd > approvalThresholdSGD {
		pm.Status = "PENDING_APPROVAL"
	}
	p.payments = append(p.payments, pm)
	p.idem[req.IdempotencyKey] = pm.ID
	p.mu.Unlock()

	if pm.Status == "PROCESSING" {
		p.execute(pm)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return http.StatusCreated, map[string]any{"payment": *pm}, nil
}

// execute sends the payment through the payment rail's services and posts it
// to the ledger only if every service accepted it.
func (p *portal) execute(pm *Payment) {
	svc := "payments"
	if pm.Rail == "PAYNOW" {
		svc = "paynow"
	}
	t := p.call(svc, pm.ID)

	p.mu.Lock()
	defer p.mu.Unlock()
	pm.Trace, pm.UpdatedAt = t, p.now()
	if !t.OK {
		pm.Status, pm.Error = "FAILED", msgPaymentFailed
		return
	}
	acct := p.account(pm.FromAccount)
	if pm.DebitAmount > acct.Balance { // balance may have moved while waiting for approval
		pm.Status, pm.Error = "FAILED", "insufficient funds at execution time"
		return
	}
	acct.Balance -= pm.DebitAmount
	pm.Status = "COMPLETED"
	p.txns = append(p.txns, Txn{
		ID: p.nextID("TXN"), AccountID: acct.ID, AccountName: acct.Name, Currency: acct.Currency, At: pm.UpdatedAt,
		Text:   fmt.Sprintf("%s to %s", railLabel(pm.Rail), pm.Beneficiary),
		Amount: -pm.DebitAmount, Balance: acct.Balance, Ref: pm.Reference,
	})
}

func railLabel(r string) string {
	return map[string]string{"PAYNOW": "PayNow", "MEPS": "MEPS+", "SWIFT": "SWIFT"}[r]
}

func (p *portal) findPayment(id string) *Payment {
	for _, pm := range p.payments {
		if pm.ID == id {
			return pm
		}
	}
	return nil
}

func (p *portal) decidePayment(approve bool) func(demoUser, *http.Request) (int, any, error) {
	return func(u demoUser, r *http.Request) (int, any, error) {
		if u.Role != "checker" {
			return 0, nil, &apiError{Status: http.StatusForbidden, Message: "only a checker can approve or reject payments"}
		}
		p.mu.Lock()
		pm := p.findPayment(r.PathValue("id"))
		switch {
		case pm == nil:
			p.mu.Unlock()
			return 0, nil, &apiError{Status: http.StatusNotFound, Message: "payment not found"}
		case pm.Status != "PENDING_APPROVAL":
			p.mu.Unlock()
			return 0, nil, &apiError{Status: http.StatusConflict, Message: "payment is " + pm.Status + ", not pending approval"}
		case pm.CreatedBy == u.Name:
			p.mu.Unlock()
			return 0, nil, &apiError{Status: http.StatusForbidden, Message: "segregation of duties: you cannot approve your own payment"}
		}
		pm.ApprovedBy, pm.UpdatedAt = u.Name, p.now()
		if !approve {
			pm.Status = "REJECTED"
			out := *pm
			p.mu.Unlock()
			return http.StatusOK, map[string]any{"payment": out}, nil
		}
		pm.Status = "PROCESSING"
		p.mu.Unlock()
		p.execute(pm)
		p.mu.Lock()
		defer p.mu.Unlock()
		return http.StatusOK, map[string]any{"payment": *pm}, nil
	}
}

func (p *portal) listLCs(u demoUser, r *http.Request) (int, any, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := []LetterOfCredit{}
	for i := len(p.lcs) - 1; i >= 0 && len(out) < maxListed; i-- {
		out = append(out, *p.lcs[i])
	}
	return http.StatusOK, map[string]any{"lcs": out}, nil
}

type lcRequest struct {
	Applicant   string `json:"applicant_account"`
	Beneficiary string `json:"beneficiary"`
	Country     string `json:"country"`
	Amount      string `json:"amount"`
	Currency    string `json:"currency"`
	Expiry      string `json:"expiry"`
	Goods       string `json:"goods"`
}

func (p *portal) createLC(u demoUser, r *http.Request) (int, any, error) {
	if u.Role != "maker" {
		return 0, nil, &apiError{Status: http.StatusForbidden, Message: "only a maker can apply for letters of credit"}
	}
	var req lcRequest
	if err := decode(r, &req); err != nil {
		return 0, nil, err
	}
	amount, err := parseAmount(req.Amount)
	if err != nil {
		return 0, nil, badRequest("%v", err)
	}
	if _, ok := fxToSGD[req.Currency]; !ok {
		return 0, nil, badRequest("unsupported currency %q", req.Currency)
	}
	if !textRe.MatchString(req.Beneficiary) || !textRe.MatchString(req.Goods) {
		return 0, nil, badRequest("beneficiary and goods description are required (max 100 characters)")
	}
	if !isoCtry.MatchString(req.Country) {
		return 0, nil, badRequest("country must be a 2-letter ISO code")
	}
	exp, err := time.Parse("2006-01-02", req.Expiry)
	if !dateRe.MatchString(req.Expiry) || err != nil || !exp.After(p.now()) || exp.After(p.now().AddDate(2, 0, 0)) {
		return 0, nil, badRequest("expiry must be a date within the next 2 years")
	}
	p.mu.Lock()
	if p.account(req.Applicant) == nil {
		p.mu.Unlock()
		return 0, nil, badRequest("unknown applicant account")
	}
	lc := &LetterOfCredit{
		ID: p.nextID("LC-SG"), Applicant: req.Applicant, Beneficiary: req.Beneficiary, Country: req.Country,
		Amount: amount, Currency: req.Currency, Expiry: req.Expiry, Goods: req.Goods, Status: "PROCESSING",
		CreatedBy: u.Name, CreatedAt: p.now(),
	}
	p.lcs = append(p.lcs, lc)
	p.mu.Unlock()

	t := p.call("trade", lc.ID)
	p.mu.Lock()
	defer p.mu.Unlock()
	lc.Trace = t
	if t.OK {
		lc.Status = "ISSUED"
	} else {
		lc.Status, lc.Error = "FAILED", msgLCFailed
	}
	return http.StatusCreated, map[string]any{"lc": *lc}, nil
}

func (p *portal) listKYC(u demoUser, r *http.Request) (int, any, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := []KYCCase{}
	for i := len(p.kyc) - 1; i >= 0 && len(out) < maxListed; i-- {
		out = append(out, *p.kyc[i])
	}
	return http.StatusOK, map[string]any{"cases": out}, nil
}

type kycRequest struct {
	Kind    string `json:"kind"`
	Entity  string `json:"entity"`
	Country string `json:"country"`
}

func (p *portal) createKYC(u demoUser, r *http.Request) (int, any, error) {
	var req kycRequest
	if err := decode(r, &req); err != nil {
		return 0, nil, err
	}
	if !contains([]string{"new-beneficiary", "new-subsidiary", "periodic-review"}, req.Kind) {
		return 0, nil, badRequest("unknown request type")
	}
	if !textRe.MatchString(req.Entity) || !isoCtry.MatchString(req.Country) {
		return 0, nil, badRequest("entity name (max 100 characters) and a 2-letter country code are required")
	}
	p.mu.Lock()
	c := &KYCCase{ID: p.nextID("KYC"), Kind: req.Kind, Entity: req.Entity, Country: req.Country,
		Status: "PROCESSING", CreatedBy: u.Name, CreatedAt: p.now()}
	p.kyc = append(p.kyc, c)
	p.mu.Unlock()

	t := p.call("kyc", c.ID)
	p.mu.Lock()
	defer p.mu.Unlock()
	c.Trace = t
	if t.OK {
		c.Status = "SUBMITTED"
	} else {
		c.Status, c.Error = "FAILED", msgKYCFailed
	}
	return http.StatusCreated, map[string]any{"case": *c}, nil
}

// status probes every backend and summarises recent customer calls.
func (p *portal) status(u demoUser, r *http.Request) (int, any, error) {
	type svcStatus struct {
		backend
		Reachable  bool    `json:"reachable"`
		ProbeError string  `json:"probe_error,omitempty"`
		Calls      int     `json:"recent_calls"`
		SuccessPct float64 `json:"success_pct"`
		AvgMs      int64   `json:"avg_latency_ms"`
		LastError  string  `json:"last_error,omitempty"`
		LastErrAt  string  `json:"last_error_at,omitempty"`
	}
	out := make([]svcStatus, len(p.backends))
	var wg sync.WaitGroup
	probe := &http.Client{Timeout: 2 * time.Second}
	for i, b := range p.backends {
		out[i].backend = b
		wg.Add(1)
		go func(s *svcStatus) {
			defer wg.Done()
			if s.URL == "" {
				s.ProbeError = "not configured"
				return
			}
			resp, err := probe.Get(s.URL + "/healthz")
			if err != nil {
				s.ProbeError = cleanErr(err)
				return
			}
			resp.Body.Close()
			s.Reachable = resp.StatusCode == http.StatusOK
			if !s.Reachable {
				s.ProbeError = fmt.Sprintf("HTTP %d", resp.StatusCode)
			}
		}(&out[i])
	}
	wg.Wait()

	p.mu.Lock()
	defer p.mu.Unlock()
	for i := range out {
		recent := p.stats[out[i].Key].recent
		var ok, total int64
		for _, t := range recent {
			total += t.LatencyMs
			if t.OK {
				ok++
			} else {
				out[i].LastError, out[i].LastErrAt = t.Error, t.At.Format(time.RFC3339)
			}
		}
		out[i].Calls = len(recent)
		if len(recent) > 0 {
			out[i].SuccessPct = float64(int(1000*float64(ok)/float64(len(recent)))) / 10
			out[i].AvgMs = total / int64(len(recent))
		}
	}
	sort.SliceStable(out, func(a, b int) bool { return out[a].Reachable && !out[b].Reachable })
	return http.StatusOK, map[string]any{"services": out, "checked_at": p.now().Format(time.RFC3339)}, nil
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------- mode entry point

func servePortal() {
	p := newPortal(map[string]string{
		"accounts": env("ACCOUNTS_URL", ""),
		"payments": env("PAYMENTS_URL", ""),
		"paynow":   env("PAYNOW_URL", ""),
		"trade":    env("TRADE_URL", ""),
		"kyc":      env("KYC_URL", ""),
	})
	srv := &http.Server{
		Addr: ":" + env("PORT", "8080"), Handler: p.routes(),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 30 * time.Second,
	}
	go func() {
		logJSON("INFO", "bank portal started", map[string]any{"port": env("PORT", "8080"), "version": version})
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logJSON("FATAL", "listen failed", map[string]any{"error": err.Error()})
			os.Exit(1)
		}
	}()
	waitForShutdown(srv)
}
