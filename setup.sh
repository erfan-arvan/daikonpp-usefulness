#!/usr/bin/env bash
# Bootstraps everything the usefulness pipeline needs that isn't already on
# the HPC: Daikon (built from source), daikonplusplus (built from source),
# and Defects4J (cloned + initialized). Idempotent: re-running skips steps
# whose output already exists.
#
# Usage:
#   ./setup.sh [scripts_root] [build_root]
#
# scripts_root defaults to the current directory (where this repo lives,
# e.g. /project/mjk76/ea442/usefullness) and is where usefulness_env.sh gets
# written. build_root is where Daikon/daikonplusplus/Defects4J are actually
# cloned and built — it defaults to /scratch/<account>/$USER/usefulness-build,
# NOT scripts_root, because /project-style GPFS/Lustre mounts on some HPC
# systems do not reliably keep the executable bit on freshly checked-out
# files (confirmed the hard way: chmod +x didn't even survive a retry on one
# such mount), while /scratch mounts normally do -- this also matches the
# existing convention in this project's own promptstudy/submit.sh, which
# already puts its Maven install under /scratch/mjk76/$USER/. Only the
# resulting .jar files (plain data, no exec bit needed) get referenced from
# scripts_root via usefulness_env.sh.
set -euo pipefail

ROOT="${1:-$PWD}"
mkdir -p "$ROOT"

BUILD_ROOT="${2:-${BUILD_ROOT:-/scratch/${SLURM_JOB_ACCOUNT:-mjk76}/$USER/usefulness-build}}"
mkdir -p "$BUILD_ROOT"

DAIKONPP_GIT_URL="${DAIKONPP_GIT_URL:-https://github.com/erfan-arvan/daikonplusplus.git}"
DAIKON_GIT_URL="${DAIKON_GIT_URL:-https://github.com/codespecs/daikon.git}"
DEFECTS4J_GIT_URL="${DEFECTS4J_GIT_URL:-https://github.com/rjust/defects4j.git}"

echo "=================================================================="
echo ">>> Usefulness pipeline setup"
echo ">>> scripts root (this repo):        $ROOT"
echo ">>> build root (Daikon/D++/D4J live here): $BUILD_ROOT"
echo "=================================================================="

########################################
# Prereq checks
########################################
# cpanm is not required here: if missing, we bootstrap a private copy from
# https://cpanmin.us below (no admin rights needed), so it's not checked here.
for tool in git java javac make perl rsync curl unzip; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "WARNING: '$tool' not found on PATH."
    echo "  - git/java/javac/make/curl/unzip are required; the script will fail without them."
    echo "  - perl is required by Defects4J's own installer."
    echo "  - rsync is required by Daikon's own Makefile (java/Makefile's daikon.jar target)."
    echo "    If your HPC doesn't have it and you can't install it, ask your admin to add it,"
    echo "    or 'module load rsync' if your site provides it as a module."
  fi
done

# Capture the ambient JDK (whatever `module load` set up before this script
# ran) before it gets shadowed on PATH by the private Java 11 below -- this
# is what daikonplusplus/Daikon should keep using (they need 17+), pinned
# explicitly as DPP_JAVA_HOME so it doesn't matter what order things land on
# PATH after sourcing usefulness_env.sh.
ORIG_JAVA_HOME="${JAVA_HOME:-}"
if [[ -z "$ORIG_JAVA_HOME" ]] && command -v java >/dev/null 2>&1; then
  ORIG_JAVA_HOME="$(dirname "$(dirname "$(command -v java)")")"
fi

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

# Kept as a safety net even though the real fix is building on $BUILD_ROOT
# instead of a /project-style mount: cheap, and harmless if never needed.
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
    echo ">>> Command failed in $dir — restoring executable bits and retrying once: $*"
    fix_exec_bits "$dir"
    ( cd "$dir" && "$@" )
  fi
}

########################################
# 1. Daikon (built from source, under BUILD_ROOT)
########################################
DAIKON_BUILD_DIR="$BUILD_ROOT/daikon"
DAIKON_JAR_DEST="$ROOT/tools/daikon.jar"
mkdir -p "$ROOT/tools"

if [[ -f "$DAIKON_JAR_DEST" ]]; then
  echo ">>> daikon.jar already present at $DAIKON_JAR_DEST, skipping"
else
  echo ">>> Cloning Daikon from $DAIKON_GIT_URL (into $BUILD_ROOT)"
  [[ -d "$DAIKON_BUILD_DIR" ]] || git clone --depth 1 "$DAIKON_GIT_URL" "$DAIKON_BUILD_DIR"
  fix_exec_bits "$DAIKON_BUILD_DIR"
  echo ">>> Building daikon.jar (this takes several minutes)"
  run_with_execfix_retry "$DAIKON_BUILD_DIR" make daikon.jar
  [[ -f "$DAIKON_BUILD_DIR/daikon.jar" ]] || { echo "ERROR: daikon.jar build did not produce a jar"; exit 1; }
  cp "$DAIKON_BUILD_DIR/daikon.jar" "$DAIKON_JAR_DEST"
  echo ">>> Built and copied: $DAIKON_JAR_DEST"
fi

########################################
# 2. daikonplusplus (built from source, kept live under BUILD_ROOT)
########################################
# Unlike daikon.jar, daikonplusplus is rebuilt by run_usefulness_bug.py on
# every invocation (./gradlew clean shadowJar), so its whole source tree
# (gradlew included) needs to keep living somewhere the exec bit sticks --
# that's DPP_DIR in usefulness_env.sh below, pointing at BUILD_ROOT.
DPP_DIR="$BUILD_ROOT/daikonplusplus"
if [[ -d "$DPP_DIR" ]]; then
  echo ">>> $DPP_DIR already exists, skipping clone (run 'git -C $DPP_DIR pull' to update)"
else
  echo ">>> Cloning daikonplusplus from $DAIKONPP_GIT_URL (into $BUILD_ROOT)"
  git clone "$DAIKONPP_GIT_URL" "$DPP_DIR"
fi
fix_exec_bits "$DPP_DIR"
echo ">>> Building daikonplusplus.jar (sanity build; run_usefulness_bug.py rebuilds it itself each run)"
JAVA_VER="$(java -version 2>&1 | grep -oE '"[0-9]+' | head -1 | tr -d '"')"
export DP_JAVA_VERSION="${DP_JAVA_VERSION:-${JAVA_VER:-17}}"
run_with_execfix_retry "$DPP_DIR" ./gradlew -q clean shadowJar
DPP_JAR="$DPP_DIR/build/libs/daikonplusplus.jar"
[[ -f "$DPP_JAR" ]] || { echo "ERROR: daikonplusplus.jar build did not produce a jar"; exit 1; }
echo ">>> Built: $DPP_JAR"

########################################
# 3. A private Java 11 for Defects4J
########################################
# Defects4J's own launcher (framework/core/Constants.pm) checks the major
# version of `java` on PATH with an EXACT-match comparison -- `if ($1 != 11)
# die`. Not "at least 11": exactly 11. Java 17/23 (or anything else) fails
# this check outright, and on a cluster with no Java 11 module at all,
# `module load` can't fix it. Rather than depend on the cluster providing
# one, download a private Temurin 11 into BUILD_ROOT if D4J_JAVA_HOME isn't
# already set to something usable.
D4J_JDK_DIR="$BUILD_ROOT/jdk11"
if [[ -n "${D4J_JAVA_HOME:-}" ]] && [[ -x "$D4J_JAVA_HOME/bin/java" ]]; then
  echo ">>> Using existing D4J_JAVA_HOME=$D4J_JAVA_HOME for Defects4J"
elif [[ -x "$D4J_JDK_DIR/bin/java" ]]; then
  echo ">>> Private JDK 11 already present at $D4J_JDK_DIR"
  D4J_JAVA_HOME="$D4J_JDK_DIR"
else
  echo ">>> No usable Java 11 found; downloading a private Temurin 11 into $D4J_JDK_DIR"
  echo ">>> Disk usage on \$BUILD_ROOT before download:"
  df -h "$BUILD_ROOT" || true
  mkdir -p "$D4J_JDK_DIR"
  JDK_TARBALL="$BUILD_ROOT/jdk11.tar.gz"
  if ! curl -fsSL "https://api.adoptium.net/v3/binary/latest/11/ga/linux/x64/jdk/hotspot/normal/eclipse?project=jdk" \
      -o "$JDK_TARBALL"; then
    echo "ERROR: curl failed to download the JDK 11 tarball (exit $?)"
    exit 1
  fi
  echo ">>> Downloaded $(ls -la "$JDK_TARBALL" 2>&1)"
  if ! tar xzf "$JDK_TARBALL" -C "$D4J_JDK_DIR" --strip-components=1; then
    echo "ERROR: tar failed to extract the JDK 11 tarball into $D4J_JDK_DIR"
    echo ">>> Disk usage on \$BUILD_ROOT at failure:"
    df -h "$BUILD_ROOT" || true
    exit 1
  fi
  rm -f "$JDK_TARBALL"
  # Some HPC filesystems don't reliably preserve the executable bit through
  # a tar extraction (the same issue fix_exec_bits works around for git
  # checkouts elsewhere in this script) -- repair it explicitly here rather
  # than assume the archive's permissions survived.
  chmod -R u+rX "$D4J_JDK_DIR"
  find "$D4J_JDK_DIR/bin" "$D4J_JDK_DIR/lib" -type f -exec chmod u+x {} \; 2>/dev/null || true
  if [[ ! -x "$D4J_JDK_DIR/bin/java" ]]; then
    echo "ERROR: JDK 11 download/extract did not produce a usable java at $D4J_JDK_DIR/bin/java"
    echo ">>> Contents of $D4J_JDK_DIR:"
    ls -la "$D4J_JDK_DIR" || true
    echo ">>> Contents of $D4J_JDK_DIR/bin (if present):"
    ls -la "$D4J_JDK_DIR/bin" 2>&1 || true
    exit 1
  fi
  D4J_JAVA_HOME="$D4J_JDK_DIR"
fi
"$D4J_JAVA_HOME/bin/java" -version
export PATH="$D4J_JAVA_HOME/bin:$PATH"

########################################
# 4. Defects4J (cloned + initialized, under BUILD_ROOT)
########################################
D4J_DIR="$BUILD_ROOT/defects4j_install"
# NOTE: project_repos/ exists in a bare `git clone` of Defects4J itself (it's
# where get_repos.sh lives) -- its presence does NOT mean init.sh has ever
# run. Use our own marker, written only after init.sh actually succeeds
# below, so a run that failed partway (e.g. cpanm missing) is correctly
# retried instead of being reported as "already initialized".
D4J_INIT_MARKER="$D4J_DIR/.usefulness_init_done"
if [[ -x "$D4J_DIR/framework/bin/defects4j" ]] && [[ -f "$D4J_INIT_MARKER" ]]; then
  echo ">>> Defects4J already initialized at $D4J_DIR, skipping"
else
  echo ">>> Cloning Defects4J from $DEFECTS4J_GIT_URL (into $BUILD_ROOT)"
  [[ -d "$D4J_DIR" ]] || git clone "$DEFECTS4J_GIT_URL" "$D4J_DIR"
  fix_exec_bits "$D4J_DIR"

  # cpanm may not be installed on this cluster at all (seen in practice: no
  # system package, no module). Rather than require the user to get one
  # installed, bootstrap a private copy of the standalone cpanm script into
  # BUILD_ROOT -- this is the officially documented way to get cpanm with no
  # admin rights (https://cpanmin.us), and it's just a Perl script, so it
  # doesn't need its own exec bit trickery: it's invoked as `perl cpanm ...`.
  CPANM="$BUILD_ROOT/cpanm"
  if command -v cpanm >/dev/null 2>&1; then
    CPANM="$(command -v cpanm)"
    CPANM_CMD=("$CPANM")
  else
    if [[ ! -f "$CPANM" ]]; then
      echo ">>> cpanm not found on PATH; bootstrapping a private copy into $BUILD_ROOT"
      curl -fsSL https://cpanmin.us -o "$CPANM"
    fi
    CPANM_CMD=(perl "$CPANM")
  fi

  echo ">>> Installing Defects4J's Perl dependencies (cpanm --installdeps .)"
  (cd "$D4J_DIR" && "${CPANM_CMD[@]}" --local-lib="$D4J_DIR/.perl5" --installdeps . )
  echo ">>> Running Defects4J's init.sh (downloads project repos + Major + test-gen libs — large, can take a while)"
  export PERL5LIB="$D4J_DIR/.perl5/lib/perl5:${PERL5LIB:-}"
  run_with_execfix_retry "$D4J_DIR" ./init.sh

  # init.sh doesn't treat every failed download inside it (e.g. a
  # get_repos.sh curl write error) as fatal to the script's own exit code,
  # so its exit 0 alone isn't proof the project repos actually landed.
  # Confirm Defects4J really works before marking it initialized.
  echo ">>> Verifying Defects4J actually works (defects4j pids)"
  PIDS_OUT="$("$D4J_DIR/framework/bin/defects4j" pids 2>&1)" || {
    echo "ERROR: 'defects4j pids' failed after init.sh:"
    echo "$PIDS_OUT"
    exit 1
  }
  [[ -n "$PIDS_OUT" ]] || {
    echo "ERROR: 'defects4j pids' returned no project IDs after init.sh --"
    echo "the project-repos archive likely didn't fully download. Check disk"
    echo "quota/usage on \$BUILD_ROOT and re-run setup.sh."
    exit 1
  }
  echo "$PIDS_OUT"

  touch "$D4J_INIT_MARKER"
  echo ">>> Defects4J initialized and verified at $D4J_DIR"
fi

########################################
# Write out an env file the submit scripts can source
########################################
ENV_FILE="$ROOT/usefulness_env.sh"
cat > "$ENV_FILE" <<EOF
# Generated by setup.sh — source this before running the experiment scripts
# (or copy these exports into submit.sh / submit_daikon.sh).
export ROOT="$ROOT"
export BUILD_ROOT="$BUILD_ROOT"
export DPP_DIR="$DPP_DIR"
export DAIKON_JAR="$DAIKON_JAR_DEST"
export PERL5LIB="$D4J_DIR/.perl5/lib/perl5:\${PERL5LIB:-}"
# Defects4J's own launcher hard-requires EXACTLY Java 11 (an exact-match
# version check in its Constants.pm, not "at least"). This cluster has no
# Java 11 module, so setup.sh downloaded a private Temurin 11. D4J_JAVA_HOME
# is read directly by run_usefulness_bug.py / run_daikon_usefulness_bug.py
# (lib_defects4j.d4j_env()) for every \`defects4j\` subprocess call they make.
export D4J_JAVA_HOME="$D4J_JAVA_HOME"
# DPP_JAVA_HOME pins the JDK daikonplusplus/Daikon build/run under (they need
# 17+) independent of D4J_JAVA_HOME/PATH below, so it doesn't matter what
# order things land on PATH after sourcing this file.
export DPP_JAVA_HOME="$ORIG_JAVA_HOME"
# Puts both defects4j itself AND the Java 11 it requires on PATH, so a plain
# interactive \`defects4j pids\` works after sourcing this file, not just the
# python scripts (which use D4J_JAVA_HOME directly and don't rely on PATH).
export PATH="$D4J_DIR/framework/bin:$D4J_JAVA_HOME/bin:\$PATH"
EOF

echo "=================================================================="
echo ">>> Setup complete."
echo ">>> daikon.jar:         $DAIKON_JAR_DEST"
echo ">>> daikonplusplus dir: $DPP_DIR (jar rebuilt per-run)"
echo ">>> defects4j:          $D4J_DIR/framework/bin/defects4j"
echo ">>> Wrote: $ENV_FILE — source it (or fold its exports into your"
echo "    submit.sh / submit_daikon.sh) before running the experiment scripts."
echo "=================================================================="
