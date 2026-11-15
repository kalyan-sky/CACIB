import unittest

from alert_coverage import analyze, render, rule_uses

AM = """
route:
  receiver: sre-agent
  routes:
    - receiver: "null"
      matchers: ['alertname=~"Watchdog|InfoInhibitor"']
receivers:
  - name: "null"
  - name: sre-agent
    webhook_configs: [{url: "<secret>"}]
"""


class AlertCoverageTest(unittest.TestCase):
    def test_rule_matching(self):
        self.assertTrue(rule_uses('sum(rate(txn_failures_total[5m]))', "bank-apps", {"txn_failures_total"}))
        self.assertTrue(rule_uses('up{job="sre-agent"} == 0', "sre-agent", set()))
        self.assertFalse(rule_uses('sum(rate(txn_failures_total_x[5m]))', "bank-apps", {"txn_failures_total"}))
        self.assertFalse(rule_uses("up == 0", "argocd-metrics", {"up"}))  # `up` alone is TargetDown's job

    def test_report(self):
        targets = [{"labels": {"job": "bank-apps"}, "health": "up"}, {"labels": {"job": "bank-apps"}, "health": "down"},
                   {"labels": {"job": "argocd-metrics"}, "health": "up"}, {"labels": {"job": "kubelet"}, "health": "up"}]
        rules = [{"name": "TargetDown", "query": "up == 0", "health": "ok", "state": "inactive"},
                 {"name": "BankAppHighFailureRate", "query": "rate(txn_failures_total[5m]) > 0.05", "health": "ok",
                  "state": "firing"},
                 {"name": "Broken", "query": "foo(", "health": "err", "lastError": "parse error", "state": "inactive"}]
        r = analyze(targets, rules, {"bank-apps": {"txn_failures_total", "up"}, "argocd-metrics": {"argocd_app_info"}}, AM)
        jobs = {j["job"]: j for j in r["jobs"]}
        self.assertEqual(jobs["bank-apps"]["signal_rules"], ["BankAppHighFailureRate"])
        self.assertEqual((jobs["bank-apps"]["up"], jobs["bank-apps"]["down"]), (1, 1))
        self.assertFalse(jobs["argocd-metrics"]["covered"])
        self.assertTrue(jobs["kubelet"]["covered"])  # infrastructure job, built-in rules
        self.assertEqual([b["name"] for b in r["broken_rules"]], ["Broken"])
        self.assertTrue(r["routing"]["ok"])
        self.assertEqual(r["routing"]["silenced"][0]["receiver"], "null")
        text = render(r)
        self.assertIn("Monitored without signal alerts: argocd-metrics", text)
        self.assertIn("Targets down: bank-apps (1)", text)

    def test_routing_not_to_agent(self):
        r = analyze([], [], {}, "route: {receiver: 'null'}\nreceivers: [{name: 'null'}]")
        self.assertFalse(r["routing"]["ok"])


if __name__ == "__main__":
    unittest.main()
