#!/usr/bin/env bash
# Prepare a Linux VM (any cloud) as the jump host for the EKS lab:
# installs git, make, python3 + PyYAML, kubectl and terraform, then prints the
# AWS identity and the public IP to put in terraform.tfvars (api_public_cidrs).
# Needs the AWS CLI already configured (aws configure / aws sso login).
set -euo pipefail

TF_VERSION="${TF_VERSION:-1.16.7}"
K8S_MINOR="${K8S_MINOR:-1.37}" # kubectl within one minor version of the cluster
BIN="$HOME/bin"
mkdir -p "$BIN"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

say "OS packages"
if command -v apt-get >/dev/null; then
  sudo apt-get -qq update
  sudo apt-get -qq install -y git make python3 python3-yaml unzip curl jq >/dev/null
elif command -v dnf >/dev/null; then
  sudo dnf -q install -y git make python3 python3-pyyaml unzip jq >/dev/null
else
  echo "no apt-get or dnf: install git, make, python3, PyYAML, unzip, curl and jq yourself" >&2
fi

case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "unsupported CPU $(uname -m)" >&2; exit 1 ;;
esac

say "kubectl ($K8S_MINOR)"
kv=$(curl -fsSL "https://dl.k8s.io/release/stable-$K8S_MINOR.txt")
curl -fsSL -o "$BIN/kubectl" "https://dl.k8s.io/release/$kv/bin/linux/$arch/kubectl"
chmod +x "$BIN/kubectl"

say "terraform $TF_VERSION"
tmp=$(mktemp -d)
curl -fsSL -o "$tmp/tf.zip" "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_${arch}.zip"
unzip -o -q "$tmp/tf.zip" terraform -d "$BIN"
rm -rf "$tmp"

grep -q 'HOME/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH=$HOME/bin:$PATH' >>"$HOME/.bashrc"
export PATH="$BIN:$PATH"

say "Versions"
kubectl version --client | head -1
terraform version | head -1
aws --version

say "AWS identity (becomes cluster-admin when you run terraform apply from here)"
aws sts get-caller-identity --output table

ip=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
say "This jump host reaches AWS from $ip"
cat <<EOF
Put this in terraform/terraform.tfvars so only this host can reach the Kubernetes API:

  api_public_cidrs = ["$ip/32"]

Make the IP static first (GCP: VPC network > IP addresses > Promote to static),
or the allow-list breaks the next time the VM restarts.
Open a new shell (or: source ~/.bashrc) so ~/bin is on your PATH.
EOF
