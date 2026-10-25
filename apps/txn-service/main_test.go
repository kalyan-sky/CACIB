package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestSummariseStatus(t *testing.T) {
	cases := []struct {
		ok, fail int
		want     string
	}{
		{0, 0, "NO DATA"},
		{100, 0, "HEALTHY"},
		{95, 5, "DEGRADED"},
		{50, 50, "DOWN"},
	}
	for _, c := range cases {
		s := &targetStats{}
		for i := 0; i < c.ok; i++ {
			s.record(true, time.Millisecond, "")
		}
		for i := 0; i < c.fail; i++ {
			s.record(false, time.Millisecond, "502 upstream down")
		}
		if got := s.summarise("t").Status; got != c.want {
			t.Errorf("%d ok / %d failed: got %s, want %s", c.ok, c.fail, got, c.want)
		}
	}
}

func TestProbe(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Write([]byte("ok"))
			return
		}
		http.Error(w, "nope", http.StatusServiceUnavailable)
	}))
	defer srv.Close()

	if rc := probe([]string{srv.URL + "/healthz"}); rc != 0 {
		t.Errorf("healthy endpoint: exit %d, want 0", rc)
	}
	if rc := probe([]string{srv.URL + "/readyz"}); rc != 1 {
		t.Errorf("503 endpoint: exit %d, want 1", rc)
	}
	if rc := probe([]string{"http://127.0.0.1:1/unreachable"}); rc != 1 {
		t.Errorf("unreachable endpoint: exit %d, want 1", rc)
	}
	if rc := probe(nil); rc != 2 {
		t.Errorf("no args: exit %d, want 2", rc)
	}
}
