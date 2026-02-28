SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE   := docker-compose
CLUSTER_A := porpulsion-cluster-a-1
CLUSTER_B := porpulsion-cluster-b-1
HELM      := porpulsion-helm-1

# kubectl via docker exec — no local kubeconfig ever needed
KUBECTL_A := docker exec $(CLUSTER_A) kubectl
KUBECTL_B := docker exec $(CLUSTER_B) kubectl

# Run helm inside the persistent helm container.
# Usage: $(call helm, K3S_CONTAINER, K3S_HOSTNAME:PORT, helm args...)
#
# The helm container is on the same Docker network as both k3s clusters,
# so it can reach them by hostname. It docker-execs into k3s to fetch
# the kubeconfig, rewrites 127.0.0.1 to the k3s service hostname,
# and writes it to /tmp inside the helm container only.
define helm
	docker exec $(HELM) sh -c "\
		docker exec $(1) cat /etc/rancher/k3s/k3s.yaml \
			| sed 's|127.0.0.1:[0-9]*|$(2)|g' \
			> /tmp/kubeconfig-$(1).yaml && \
		chmod 600 /tmp/kubeconfig-$(1).yaml && \
		KUBECONFIG=/tmp/kubeconfig-$(1).yaml helm $(3) \
	"
endef

.PHONY: help deploy redeploy teardown clean-ns _clean-cluster status logs stream

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

deploy: ## Full deploy: start clusters, build image, helm install both agents
	@echo ""
	@echo "==> Starting clusters + helm runner..."
	$(COMPOSE) up -d
	@echo "Waiting for cluster-a API..."
	@until $(KUBECTL_A) get nodes &>/dev/null; do sleep 2; done
	@echo "  cluster-a ready"
	@echo "Waiting for cluster-b API..."
	@until $(KUBECTL_B) get nodes &>/dev/null; do sleep 2; done
	@echo "  cluster-b ready"

	@echo ""
	@echo "==> Building porpulsion-agent image..."
	docker build -t porpulsion-agent:local .

	@echo ""
	@echo "==> Loading image into clusters..."
	docker save porpulsion-agent:local | docker exec -i $(CLUSTER_A) ctr images import -
	docker save porpulsion-agent:local | docker exec -i $(CLUSTER_B) ctr images import -

	@echo ""
	@echo "==> Helm installing porpulsion on cluster-a..."
	@IP_A=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(CLUSTER_A)); \
	echo "  cluster-a IP: $$IP_A"; \
	docker exec $(HELM) sh -c " \
		docker exec $(CLUSTER_A) cat /etc/rancher/k3s/k3s.yaml \
			| sed 's|127.0.0.1:[0-9]*|cluster-a:6443|g' \
			> /tmp/kubeconfig-a.yaml && \
		chmod 600 /tmp/kubeconfig-a.yaml && \
		KUBECONFIG=/tmp/kubeconfig-a.yaml helm upgrade --install porpulsion /charts/porpulsion \
			--create-namespace --namespace porpulsion \
			--set agent.agentName=cluster-a \
			--set agent.selfUrl=https://$$IP_A:30443 \
			--set agent.image=porpulsion-agent:local \
			--set agent.pullPolicy=Never \
			--set service.type=NodePort \
			--set service.uiNodePort=30080 \
			--set service.agentNodePort=30443 \
			--wait --timeout 90s \
	"

	@echo ""
	@echo "==> Helm installing porpulsion on cluster-b..."
	@IP_B=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(CLUSTER_B)); \
	echo "  cluster-b IP: $$IP_B"; \
	docker exec $(HELM) sh -c " \
		docker exec $(CLUSTER_B) cat /etc/rancher/k3s/k3s.yaml \
			| sed 's|127.0.0.1:[0-9]*|cluster-b:6444|g' \
			> /tmp/kubeconfig-b.yaml && \
		chmod 600 /tmp/kubeconfig-b.yaml && \
		KUBECONFIG=/tmp/kubeconfig-b.yaml helm upgrade --install porpulsion /charts/porpulsion \
			--create-namespace --namespace porpulsion \
			--set agent.agentName=cluster-b \
			--set agent.selfUrl=https://$$IP_B:30443 \
			--set agent.image=porpulsion-agent:local \
			--set agent.pullPolicy=Never \
			--set service.type=NodePort \
			--set service.uiNodePort=30080 \
			--set service.agentNodePort=30443 \
			--wait --timeout 90s \
	"

	@echo ""
	@echo "============================================"
	@echo "  porpulsion is running!"
	@echo "============================================"
	@echo ""
	@echo "  cluster-a UI:    http://localhost:8001"
	@echo "  cluster-b UI:    http://localhost:8002"
	@echo ""
	@echo "  kubectl:"
	@echo "    docker exec $(CLUSTER_A) kubectl get pods -n porpulsion"
	@echo "    docker exec $(CLUSTER_B) kubectl get pods -n porpulsion"
	@echo ""

redeploy: ## Rebuild agent image + helm upgrade (clusters must already be running)
	@echo ""
	@echo "==> Rebuilding porpulsion-agent image..."
	docker build -t porpulsion-agent:local .
	@echo ""
	@echo "==> Loading image into clusters..."
	docker save porpulsion-agent:local | docker exec -i $(CLUSTER_A) ctr images import -
	docker save porpulsion-agent:local | docker exec -i $(CLUSTER_B) ctr images import -
	@echo ""
	@echo "==> Helm upgrading cluster-a..."
	@IP_A=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(CLUSTER_A)); \
	echo "  cluster-a IP: $$IP_A"; \
	docker exec $(HELM) sh -c " \
		docker exec $(CLUSTER_A) cat /etc/rancher/k3s/k3s.yaml \
			| sed 's|127.0.0.1:[0-9]*|cluster-a:6443|g' \
			> /tmp/kubeconfig-a.yaml && \
		chmod 600 /tmp/kubeconfig-a.yaml && \
		KUBECONFIG=/tmp/kubeconfig-a.yaml helm upgrade --install porpulsion /charts/porpulsion \
			--create-namespace --namespace porpulsion \
			--set agent.agentName=cluster-a \
			--set agent.selfUrl=https://$$IP_A:30443 \
			--set agent.image=porpulsion-agent:local \
			--set agent.pullPolicy=Never \
			--set service.type=NodePort \
			--set service.uiNodePort=30080 \
			--set service.agentNodePort=30443 \
			--wait --timeout 90s \
	"
	@echo ""
	@echo "==> Helm upgrading cluster-b..."
	@IP_B=$$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(CLUSTER_B)); \
	echo "  cluster-b IP: $$IP_B"; \
	docker exec $(HELM) sh -c " \
		docker exec $(CLUSTER_B) cat /etc/rancher/k3s/k3s.yaml \
			| sed 's|127.0.0.1:[0-9]*|cluster-b:6444|g' \
			> /tmp/kubeconfig-b.yaml && \
		chmod 600 /tmp/kubeconfig-b.yaml && \
		KUBECONFIG=/tmp/kubeconfig-b.yaml helm upgrade --install porpulsion /charts/porpulsion \
			--create-namespace --namespace porpulsion \
			--set agent.agentName=cluster-b \
			--set agent.selfUrl=https://$$IP_B:30443 \
			--set agent.image=porpulsion-agent:local \
			--set agent.pullPolicy=Never \
			--set service.type=NodePort \
			--set service.uiNodePort=30080 \
			--set service.agentNodePort=30443 \
			--wait --timeout 90s \
	"
	@echo ""
	@echo "  Done. Agents redeployed."
	@echo "  cluster-a UI: http://localhost:8001"
	@echo "  cluster-b UI: http://localhost:8002"
	@echo ""

teardown: ## Destroy clusters and volumes
	$(COMPOSE) down -v

status: ## Show pods, deployments, and peer status
	@echo "=== Cluster A Pods ==="
	@$(KUBECTL_A) -n porpulsion get pods 2>/dev/null || echo "  not available"
	@echo ""
	@echo "=== Cluster A Deployments ==="
	@$(KUBECTL_A) -n porpulsion get deployments 2>/dev/null || echo "  not available"
	@echo ""
	@echo "=== Cluster B Pods ==="
	@$(KUBECTL_B) -n porpulsion get pods 2>/dev/null || echo "  not available"
	@echo ""
	@echo "=== Cluster B Deployments ==="
	@$(KUBECTL_B) -n porpulsion get deployments 2>/dev/null || echo "  not available"
	@echo ""
	@echo "=== Agent Peers ==="
	@curl -s http://localhost:8001/peers 2>/dev/null || echo "  cluster-a not reachable"
	@echo ""
	@curl -s http://localhost:8002/peers 2>/dev/null || echo "  cluster-b not reachable"

logs: ## Stream live logs from both clusters (Ctrl-C to stop)
	@$(KUBECTL_A) -n porpulsion logs -l app=porpulsion-agent -f --tail=20 2>/dev/null | sed 's/^/\x1b[36m[A]\x1b[0m /' & \
	$(KUBECTL_B) -n porpulsion logs -l app=porpulsion-agent -f --tail=20 2>/dev/null | sed 's/^/\x1b[33m[B]\x1b[0m /' & \
	trap 'kill 0' INT; wait

clean-ns: ## Remove porpulsion namespace from both clusters (handles CRD finalizers)
	@$(MAKE) --no-print-directory _clean-cluster KUBECTL="$(KUBECTL_A)" CLUSTER=$(CLUSTER_A) APIHOST=cluster-a:6443
	@$(MAKE) --no-print-directory _clean-cluster KUBECTL="$(KUBECTL_B)" CLUSTER=$(CLUSTER_B) APIHOST=cluster-b:6444

# Internal: clean one cluster's porpulsion namespace safely.
# Caller must pass: KUBECTL, CLUSTER, APIHOST (e.g. cluster-a:6443)
_clean-cluster:
	@$(KUBECTL) -n porpulsion get secret sh.helm.release.v1.porpulsion.v1 &>/dev/null 2>&1 && \
		docker exec $(HELM) sh -c " \
			docker exec $(CLUSTER) cat /etc/rancher/k3s/k3s.yaml \
				| sed 's|127.0.0.1:[0-9]*|$(APIHOST)|g' \
				> /tmp/kubeconfig-clean-$(CLUSTER).yaml && \
			chmod 600 /tmp/kubeconfig-clean-$(CLUSTER).yaml && \
			KUBECONFIG=/tmp/kubeconfig-clean-$(CLUSTER).yaml \
			helm uninstall porpulsion --namespace porpulsion --ignore-not-found 2>/dev/null \
		" || true
	@$(KUBECTL) get remoteapps.porpulsion.io -n porpulsion \
		-o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
		| while read name; do \
			[ -z "$$name" ] && continue; \
			$(KUBECTL) patch remoteapp.porpulsion.io "$$name" -n porpulsion \
				--type=json -p '[{"op":"remove","path":"/metadata/finalizers"}]' \
				2>/dev/null || true; \
			$(KUBECTL) delete remoteapp.porpulsion.io "$$name" -n porpulsion \
				--ignore-not-found=true 2>/dev/null || true; \
		done
	@$(KUBECTL) delete crd remoteapps.porpulsion.io --ignore-not-found=true 2>/dev/null || true
	@$(KUBECTL) delete namespace porpulsion --ignore-not-found=true 2>/dev/null || true
