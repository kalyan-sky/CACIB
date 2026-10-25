# Bank platform simulation - common tasks. Run `make help`.
PROFILE ?= $(shell cat .sim-state/profile 2>/dev/null || echo lite)
PY      ?= python3
CLUSTER ?= bank-sim
# setup.sh / eks-up.sh record which cluster and image this checkout targets
IMAGE   ?= $(shell cat .sim-state/image 2>/dev/null || echo bank-sim/txn-service:1.4.0)
CONTEXT ?= $(shell cat .sim-state/context 2>/dev/null || echo kind-$(CLUSTER))

.PHONY: help up down eks-up eks-down render apply diff build audit audit-offline capacity inventory status test

help: ## show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

up: ## create the kind cluster and deploy everything (PROFILE=lite|full)
	PROFILE=$(PROFILE) ./scripts/setup.sh

down: ## delete the kind cluster
	./scripts/teardown.sh

eks-up: ## run on Amazon EKS instead (billable! AWS_REGION, PROFILE, INSTANCE_TYPE)
	PROFILE=$(PROFILE) ./scripts/eks-up.sh

eks-down: ## delete the EKS cluster and ECR repository
	./scripts/eks-down.sh

render: ## inventory -> rendered/ manifests
	$(PY) tools/platform.py render --profile $(PROFILE) --image $(IMAGE)

diff: ## preview what `make apply` would change in the cluster
	kubectl --context $(CONTEXT) diff -R -f rendered/ || true

apply: ## apply rendered/ to the cluster
	kubectl --context $(CONTEXT) apply -R -f rendered/

build: ## rebuild the app image and load it into kind (EKS: re-run make eks-up)
	docker build -t bank-sim/txn-service:1.4.0 -t bank-sim/txn-service:latest apps/txn-service
	kind load docker-image --name $(CLUSTER) bank-sim/txn-service:1.4.0 bank-sim/txn-service:latest

audit: ## platform control audit of the live cluster
	$(PY) tools/platform.py audit --live

audit-offline: render ## platform control audit of rendered/ (no cluster needed)
	$(PY) tools/platform.py audit --path rendered

capacity: ## requests vs quota per namespace (live)
	$(PY) tools/platform.py capacity --live

inventory: ## summary of the application estate and blast radius
	$(PY) tools/platform.py inventory

status: ## estate health and customer-facing SLOs
	./sim.sh status

test: ## offline checks: Go tests, render both profiles, audit, script syntax
	cd apps/txn-service && go vet ./... && go test ./...
	$(PY) tools/platform.py render --profile full --out /tmp/bank-sim-full >/dev/null
	$(PY) tools/platform.py audit --path /tmp/bank-sim-full >/dev/null
	$(PY) tools/platform.py render --profile lite --out /tmp/bank-sim-lite
	$(PY) tools/platform.py audit --path /tmp/bank-sim-lite >/dev/null
	@for f in sim.sh scripts/*.sh scenarios/*/*.sh; do bash -n $$f || exit 1; done
	@if command -v kubeconform >/dev/null; then kubeconform -strict -summary /tmp/bank-sim-full; fi
	@echo "all offline checks passed"
