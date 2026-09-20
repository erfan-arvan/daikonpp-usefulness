#!/usr/bin/env bash
# Bootstraps everything the usefulness pipeline needs that isn't already on
# the HPC: Daikon (built from source), daikonplusplus (built from source),
# and Defects4J (cloned + initialized). Idempotent: re-running skips steps
# whose output already exists.
#
# Usage:
#   ./setup.sh [install_root]
#
# install_root defaults to the current directory. After this finishes,
# source the printed env-vars file (or copy its export lines into your
# submit.sh / submit_daikon.sh) before running the experiment scripts.
set -euo pipefail

ROOT="${1:-$PWD}"
mkdir -p "$ROOT"
cd "$ROOT"

DAIKONPP_GIT_URL="${DAIKONPP_GIT_URL:-https://github.com/erfan-arvan/daikonplusplus.git}"
DAIKON_GIT_URL="${DAIKON_GIT_URL:-https://github.com/codespecs/daikon.git}"
DEFECTS4J_GIT_URL="${DEFECTS4J_GIT_URL:-https://github.com/rjust/defects4j.git}"

echo "=================================================================="
echo ">>> Usefulness pipeline setup — install root: $ROOT"
echo "=================================================================="

########################################
# Prereq checks
########################################
for tool in git java javac make perl cpanm rsync curl unzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "WARNING: '$tool' not found on PATH."
    echo "  - git/java/javac/make/curl/unzip are required; the script will fail without them."
    echo "  - perl/cpanm are required by Defects4J's own installer."
    echo "  - rsync is required by Daikon's own Makefile (java/Makefile's daikon.jar target)."
    echo "    If your HPC doesn't have it and you can't install it, ask your admin to add it,"
    echo "    or 'module load rsync' if your site provides it as a module."
  fi
done

# A stray JAVA_TOOL_OPTIONS can corrupt `javac -version` parsing that several
# of these build systems rely on (confirmed while developing this script: a
# proxy-injected JAVA_TOOL_OPTIONS made `javac -version | head -1` pick up an
# unrelated "Picked up JAVA_TOOL_OPTIONS: ..." line instead, which broke
# Daikon's Makefile version detection). Almost certainly a non-issue on a
# plain HPC login/compute node, but cheap to guard against.
if [[ -n "${JAVA_TOOL_OPTIONS:-}" ]]; then
  echo ">>> Unsetting JAVA_TOOL_OPTIONS for this setup run (was: $JAVA_TOOL_OPTIONS)"
  unset JAVA_TOOL_OPTIONS
fi

# Some HPC filesystems (GPFS/Lustre /project mounts in particular) don't
# reliably apply git's recorded executable bit on checkout, so freshly
# cloned helper scripts (Daikon's scripts/java-cpp, its auto-cloned
# plume-scripts, Defects4J's framework/bin/defects4j, gradlew, etc.) can
# land without +x even though the repo says they should have it. Rather
# than fail on that, restore +x on every plausible script under a tree and
# retry once.
fix_exec_bits() {
  find "$1" -type f \( \
      -name '*.sh' -o -name '*.pl' -o -name '*.pm' -o -name 'gradlew' \
      -o -name 'defects4j' -o -name 'java-cpp' -o -name 'init.sh' \
      -o -path '*/plume-scripts/*' -o -path '*/scripts/*' -o -path '*/bin/*' \
    \) -exec chmod +x {} \; 2>/dev/null || true
}

run_with_execfix_retry() {
  local dir="$1"
  shift
  if ! ( cd "$dir" && "$@" ); then
    echo ">>> Command failed in $dir — restoring executable bits that this filesystem"
    echo "    may have stripped from the git checkout, and retrying once: $*"
    fix_exec_bits "$dir"
    ( cd "$dir" && "$@" )
  fi
}

########################################
# 1. Daikon (built from source)
########################################
DAIKON_DIR="$ROOT/daikon"
if [[ -f "$DAIKON_DIR/daikon.jar" ]]; then
  echo ">>> daikon.jar already built at $DAIKON_DIR/daikon.jar, skipping"
else
  echo ">>> Cloning Daikon from $DAIKON_GIT_URL"
  [[ -d "$DAIKON_DIR" ]] || git clone --depth 1 "$DAIKON_GIT_URL" "$DAIKON_DIR"
  fix_exec_bits "$DAIKON_DIR"
  echo ">>> Building daikon.jar (this takes several minutes)"
  run_with_execfix_retry "$DAIKON_DIR" make daikon.jar
  [[ -f "$DAIKON_DIR/daikon.jar" ]] || { echo "ERROR: daikon.jar build did not produce a jar"; exit 1; }
  echo ">>> Built: $DAIKON_DIR/daikon.jar"
fi

########################################
# 2. daikonplusplus (built from source)
########################################
DPP_DIR="$ROOT/daikonplusplus"
if [[ -d "$DPP_DIR" ]]; then
  echo ">>> $DPP_DIR already exists, skipping clone (run 'git -C $DPP_DIR pull' to update)"
else
  echo ">>> Cloning daikonplusplus from $DAIKONPP_GIT_URL"
  git clone "$DAIKONPP_GIT_URL" "$DPP_DIR"
fi
fix_exec_bits "$DPP_DIR"
echo ">>> Building daikonplusplus.jar"
JAVA_VER="$(java -version 2>&1 | grep -oE '"[0-9]+' | head -1 | tr -d '"')"
export DP_JAVA_VERSION="${DP_JAVA_VERSION:-${JAVA_VER:-17}}"
run_with_execfix_retry "$DPP_DIR" ./gradlew -q clean shadowJar
DPP_JAR="$DPP_DIR/build/libs/daikonplusplus.jar"
[[ -f "$DPP_JAR" ]] || { echo "ERROR: daikonplusplus.jar build did not produce a jar"; exit 1; }
echo ">>> Built: $DPP_JAR"

########################################
# 3. Defects4J (cloned + initialized)
########################################
D4J_DIR="$ROOT/defects4j_install"
if [[ -x "$D4J_DIR/framework/bin/defects4j" ]] && [[ -d "$D4J_DIR/project_repos" ]]; then
  echo ">>> Defects4J already initialized at $D4J_DIR, skipping"
else
  echo ">>> Cloning Defects4J from $DEFECTS4J_GIT_URL"
  [[ -d "$D4J_DIR" ]] || git clone "$DEFECTS4J_GIT_URL" "$D4J_DIR"
  fix_exec_bits "$D4J_DIR"
  echo ">>> Installing Defects4J's Perl dependencies (cpanm --installdeps .)"
  (cd "$D4J_DIR" && cpanm --local-lib="$D4J_DIR/.perl5" --installdeps . )
  echo ">>> Running Defects4J's init.sh (downloads project repos + Major + test-gen libs — large, can take a while)"
  export PERL5LIB="$D4J_DIR/.perl5/lib/perl5:${PERL5LIB:-}"
  run_with_execfix_retry "$D4J_DIR" ./init.sh
  echo ">>> Defects4J initialized at $D4J_DIR"
fi

########################################
# Write out an env file the submit scripts can source
########################################
ENV_FILE="$ROOT/usefulness_env.sh"
cat > "$ENV_FILE" <<EOF
# Generated by setup.sh — source this before running the experiment scripts
# (or copy these exports into submit.sh / submit_daikon.sh).
export ROOT="$ROOT"
export DPP_DIR="$DPP_DIR"
export DAIKON_JAR="$DAIKON_DIR/daikon.jar"
export PATH="$D4J_DIR/framework/bin:\$PATH"
export PERL5LIB="$D4J_DIR/.perl5/lib/perl5:\${PERL5LIB:-}"
# If your cluster's default \`module load\` JDK is not new enough for
# Defects4J (its own docs say v2.x wants Java 8) or not the one
# daikonplusplus/Daikon need (17+), set these to pin each side separately:
# export D4J_JAVA_HOME=/path/to/jdk8
# export DPP_JAVA_HOME=/path/to/jdk17
EOF

echo "=================================================================="
echo ">>> Setup complete."
echo ">>> daikon.jar:         $DAIKON_DIR/daikon.jar"
echo ">>> daikonplusplus.jar: $DPP_JAR"
echo ">>> defects4j:          $D4J_DIR/framework/bin/defects4j"
echo ">>> Wrote: $ENV_FILE — source it (or fold its exports into your"
echo "    submit.sh / submit_daikon.sh) before running the experiment scripts."
echo "=================================================================="
