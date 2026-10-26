package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeService mimics a txn-service edge app; fail=true makes it return 502.
func fakeService(t *testing.T, name string, fail *bool) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if *fail {
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(map[string]any{"error": "upstream fraud-screening: timeout", "path": []string{name}})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"path": []string{name, "core-ledger"}})
	}))
	t.Cleanup(srv.Close)
	return srv
}

type harness struct {
	t    *testing.T
	srv  *httptest.Server
	p    *portal
	down map[string]*bool
}

func newHarness(t *testing.T) *harness {
	h := &harness{t: t, down: map[string]*bool{}}
	urls := map[string]string{}
	for _, k := range []string{"accounts", "payments", "paynow", "trade", "kyc"} {
		f := false
		h.down[k] = &f
		urls[k] = fakeService(t, k, &f).URL
	}
	h.p = newPortal(urls)
	h.srv = httptest.NewServer(h.p.routes())
	t.Cleanup(h.srv.Close)
	return h
}

func (h *harness) do(user, method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	var buf bytes.Buffer
	if body != nil {
		json.NewEncoder(&buf).Encode(body)
	}
	req, _ := http.NewRequest(method, h.srv.URL+path, &buf)
	req.Header.Set("X-Demo-User", user)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

func (h *harness) balance(id string) int64 {
	h.p.mu.Lock()
	defer h.p.mu.Unlock()
	return h.p.account(id).Balance
}

func pay(amount, rail, bene, ccy, key string) map[string]any {
	return map[string]any{"from_account": "sgd-ops", "beneficiary_id": bene, "rail": rail,
		"amount": amount, "currency": ccy, "reference": "INV-1", "idempotency_key": key}
}

func payment(body map[string]any) map[string]any { return body["payment"].(map[string]any) }

func TestPaymentDebitsAccountOnSuccess(t *testing.T) {
	h := newHarness(t)
	before := h.balance("sgd-ops")
	code, body := h.do("alex", "POST", "/api/payments", pay("1,250.50", "PAYNOW", "pacific-freight", "SGD", "key-00000001"))
	if code != http.StatusCreated || payment(body)["status"] != "COMPLETED" {
		t.Fatalf("got %d %v", code, body)
	}
	if got := before - h.balance("sgd-ops"); got != 125050 {
		t.Errorf("debited %d minor units, want 125050", got)
	}
	if path := payment(body)["trace"].(map[string]any)["path"].([]any); path[0] != "paynow" {
		t.Errorf("PayNow should go through paynow-fast-adapter, trace path %v", path)
	}
}

func TestFailedPaymentDoesNotDebit(t *testing.T) {
	h := newHarness(t)
	*h.down["payments"] = true
	before := h.balance("sgd-ops")
	_, body := h.do("alex", "POST", "/api/payments", pay("900", "MEPS", "changi-warehousing", "SGD", "key-00000002"))
	p := payment(body)
	if p["status"] != "FAILED" || strings.Contains(p["error"].(string), "fraud-screening") {
		t.Fatalf("want FAILED with a customer-safe message, got %v", p)
	}
	if tr := p["trace"].(map[string]any); !strings.Contains(tr["error"].(string), "fraud-screening") {
		t.Errorf("the technical cause belongs in the trace: %v", tr)
	}
	if h.balance("sgd-ops") != before {
		t.Error("a failed payment must not move money")
	}
}

func TestIdempotencyKeyPreventsDoublePayment(t *testing.T) {
	h := newHarness(t)
	before := h.balance("sgd-ops")
	req := pay("100", "PAYNOW", "pacific-freight", "SGD", "same-key-123")
	h.do("alex", "POST", "/api/payments", req)
	code, body := h.do("alex", "POST", "/api/payments", req)
	if code != http.StatusOK || body["duplicate"] != true {
		t.Fatalf("retry should be recognised as duplicate, got %d %v", code, body)
	}
	if got := before - h.balance("sgd-ops"); got != 10000 {
		t.Errorf("debited %d, want a single 10000", got)
	}
}

func TestMakerCheckerAndSegregationOfDuties(t *testing.T) {
	h := newHarness(t)
	before := h.balance("sgd-ops")
	_, body := h.do("alex", "POST", "/api/payments", pay("75000", "MEPS", "changi-warehousing", "SGD", "key-00000003"))
	p := payment(body)
	if p["status"] != "PENDING_APPROVAL" || h.balance("sgd-ops") != before {
		t.Fatalf("above-threshold payment must wait for approval without debiting: %v", p)
	}
	id := p["id"].(string)
	if code, _ := h.do("alex", "POST", "/api/payments/"+id+"/approve", nil); code != http.StatusForbidden {
		t.Errorf("maker approving: got %d, want 403", code)
	}
	if code, _ := h.do("priya", "POST", "/api/payments", pay("10", "PAYNOW", "pacific-freight", "SGD", "key-00000004")); code != http.StatusForbidden {
		t.Errorf("checker initiating: got %d, want 403", code)
	}
	code, body := h.do("priya", "POST", "/api/payments/"+id+"/approve", nil)
	if code != http.StatusOK || payment(body)["status"] != "COMPLETED" || payment(body)["approved_by"] != "Priya Nair" {
		t.Fatalf("checker approval: %d %v", code, body)
	}
	if got := before - h.balance("sgd-ops"); got != 7_500_000 {
		t.Errorf("debited %d after approval, want 7500000", got)
	}
	if code, _ := h.do("priya", "POST", "/api/payments/"+id+"/approve", nil); code != http.StatusConflict {
		t.Errorf("approving twice: got %d, want 409", code)
	}
}

func TestPaymentValidation(t *testing.T) {
	h := newHarness(t)
	cases := []struct {
		name string
		req  map[string]any
		want int
	}{
		{"PayNow above FAST limit", pay("200000.01", "PAYNOW", "pacific-freight", "SGD", "key-00000010"), 400},
		{"MEPS in USD", pay("10", "MEPS", "pacific-freight", "USD", "key-00000011"), 400},
		{"rail not offered by beneficiary", pay("10", "SWIFT", "pacific-freight", "SGD", "key-00000012"), 400},
		{"three decimals", pay("1.005", "PAYNOW", "pacific-freight", "SGD", "key-00000013"), 400},
		{"negative", pay("-5", "PAYNOW", "pacific-freight", "SGD", "key-00000014"), 400},
		{"insufficient funds", pay("9999999", "MEPS", "pacific-freight", "SGD", "key-00000015"), 422},
		{"script in reference", map[string]any{"from_account": "sgd-ops", "beneficiary_id": "pacific-freight", "rail": "PAYNOW",
			"amount": "1", "currency": "SGD", "reference": "<script>", "idempotency_key": "key-00000016"}, 400},
		{"missing idempotency key", pay("1", "PAYNOW", "pacific-freight", "SGD", ""), 400},
	}
	for _, c := range cases {
		if code, body := h.do("alex", "POST", "/api/payments", c.req); code != c.want {
			t.Errorf("%s: got %d %v, want %d", c.name, code, body, c.want)
		}
	}
	if code, _ := h.do("mallory", "GET", "/api/accounts", nil); code != http.StatusUnauthorized {
		t.Errorf("unknown user: got %d, want 401", code)
	}
}

func TestSwiftFXConversion(t *testing.T) {
	h := newHarness(t)
	before := h.balance("sgd-ops")
	_, body := h.do("alex", "POST", "/api/payments", pay("1000", "SWIFT", "shenzhen-components", "USD", "key-00000020"))
	p := payment(body)
	if p["status"] != "COMPLETED" || p["debit_currency"] != "SGD" || p["debit_minor"].(float64) != 135000 {
		t.Fatalf("USD 1,000 from an SGD account should debit SGD 1,350.00: %v", p)
	}
	if before-h.balance("sgd-ops") != 135000 {
		t.Error("SGD account not debited by the converted amount")
	}
}

func TestAccountsUnavailableWhenAccountServiceDown(t *testing.T) {
	h := newHarness(t)
	*h.down["accounts"] = true
	code, body := h.do("alex", "GET", "/api/accounts", nil)
	if code != http.StatusServiceUnavailable || body["error"] != "Accounts unavailable" || body["trace"] == nil {
		t.Fatalf("got %d %v", code, body)
	}
}

func TestStatusAndSecurityHeaders(t *testing.T) {
	h := newHarness(t)
	*h.down["kyc"] = true
	h.do("alex", "POST", "/api/kyc", map[string]any{"kind": "new-beneficiary", "entity": "Mekong Agri", "country": "VN"})
	_, body := h.do("alex", "GET", "/api/status", nil)
	for _, s := range body["services"].([]any) {
		m := s.(map[string]any)
		if m["key"] == "kyc" && (m["success_pct"].(float64) != 0 || m["last_error"] == "") {
			t.Errorf("kyc status should show the failed request: %v", m)
		}
	}
	resp, err := http.Get(h.srv.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if !strings.Contains(resp.Header.Get("Content-Security-Policy"), "frame-ancestors 'none'") ||
		resp.Header.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("missing security headers: %v", resp.Header)
	}
}

func TestParseAmount(t *testing.T) {
	for in, want := range map[string]int64{"1": 100, "1.5": 150, "1,234.56": 123456, "0.01": 1} {
		if got, err := parseAmount(in); err != nil || got != want {
			t.Errorf("parseAmount(%q) = %d, %v; want %d", in, got, err, want)
		}
	}
	for _, in := range []string{"", "0", "abc", "1e5", "1.234", "-1", "100000000000"} {
		if _, err := parseAmount(in); err == nil {
			t.Errorf("parseAmount(%q) should fail", in)
		}
	}
}
