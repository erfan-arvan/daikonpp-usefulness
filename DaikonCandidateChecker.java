import daikon.Daikon;
import daikon.FileIO;
import daikon.PptMap;
import daikon.PptRelation;
import daikon.PptSlice;
import daikon.PptSlice1;
import daikon.PptSlice2;
import daikon.PptTopLevel;
import daikon.PrintInvariants;
import daikon.ValueTuple;
import daikon.VarInfo;
import daikon.inv.Equality;
import daikon.inv.Invariant;
import daikon.inv.InvariantStatus;
import daikon.inv.binary.BinaryInvariant;
import daikon.inv.filter.InvariantFilters;
import daikon.inv.ternary.TernaryInvariant;
import daikon.inv.unary.UnaryInvariant;
import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Checks the FROZEN invariants of one .inv file against one or more .dtrace files and reports,
 * for every invariant in every ppt's views, how many samples it was evaluated on and how many
 * violated it.
 *
 * <p>Why not the stock {@code daikon.tools.InvariantChecker}: it calls the mutating {@code
 * Invariant.add_sample}, drops an invariant from its active set after the first failure, and only
 * keeps the sets testedInvariants/failedInvariants (package-private, no per-invariant counts). It
 * cannot say how many samples an invariant was evaluated on, so HELD vs. UNEXERCISED can't be told
 * apart from its output. This class is a minimal wrapper that follows InvariantCheckProcessor's
 * sample handling but uses the non-mutating {@code check} methods, so the invariants stay exactly
 * as inferred and every violation is counted.
 *
 * <p>Samples are the ones Daikon's own inference uses (FileIO.process_sample with the dataflow
 * hierarchy adds only leaf samples -- numbered EXITs -- and builds ENTER, combined EXIT, OBJECT and
 * CLASS invariants from them through the hierarchy):
 *
 * <ul>
 *   <li>An ENTER sample is checked only when its call returns (its EXIT is read), as the stock
 *       checker does. ENTER samples of calls that never return (threw, or the trace ended) are
 *       never part of Daikon's inference; they are checked at the end as DIAGNOSTICS only, counted
 *       separately and never mixed into the eligible counts.
 *   <li>A numbered EXITnn sample is also checked at the combined EXIT (with get_missingOutOfBounds,
 *       as the stock checker does).
 *   <li>The ENTER sample of a returned call and the combined EXIT sample are propagated along the
 *       PARENT/USER relations of the hierarchy (method to OBJECT, OBJECT to CLASS), which is how
 *       Daikon's OBJECT/CLASS invariants get their data. The stock checker never evaluates
 *       OBJECT/CLASS invariants.
 * </ul>
 *
 * <p>For mapping to PrintInvariants output, each invariant is also printed exactly as
 * PrintInvariants prints it ({@code createGuardedInvariant(false)} then {@code
 * PrintInvariants.print_invariant}), and whether PrintInvariants' default filters keep it.
 *
 * <p>Usage: {@code java -cp daikon.jar:<dir> DaikonCandidateChecker --inv F.inv.gz --out
 * records.jsonl --summary summary.json [--max-violations N] [--config_option k=v]...
 * trace.dtrace.gz...}
 */
public class DaikonCandidateChecker {

  /**
   * Per-invariant counts. Eligible (Daikon's inference samples): direct = a sample of this ppt (or
   * EXITnn applied to the combined EXIT), the stock checker's path; propagated = reached this
   * OBJECT/CLASS ppt through the hierarchy. Diagnostic only: samples rooted at an ENTER whose call
   * never returned, directly (unmatched_entry) or propagated (propagated_unmatched_entry).
   */
  static final class Stats {
    long evaluations = 0;
    long violationsDirect = 0;
    long violationsPropagated = 0;
    long violationsAllNaN = 0; // eligible violations where every compared value is NaN
    long skippedMissing = 0; // eligible sample skipped: a variable's value is missing
    long skippedOutOfBounds = 0; // eligible sample skipped: a derived variable is out of bounds
    List<String> firstViolations = new ArrayList<>();
    long diagEvaluations = 0;
    long diagViolationsUnmatchedEntry = 0;
    long diagViolationsPropagatedUnmatchedEntry = 0;
    List<String> firstDiagViolations = new ArrayList<>();
  }

  static final class EnterCall {
    final PptTopLevel ppt;
    final ValueTuple vt;
    final int linenum;
    final String file;

    EnterCall(PptTopLevel ppt, ValueTuple vt, int linenum, String file) {
      this.ppt = ppt;
      this.vt = vt;
      this.linenum = linenum;
      this.file = file;
    }
  }

  static final IdentityHashMap<Invariant, Stats> stats = new IdentityHashMap<>();
  static int maxViolations = 5;
  static long samplesRead = 0;
  static long enterSamples = 0;
  static long exitWithoutEnter = 0;
  static long propagatedSamples = 0;
  static long unmappedParentVars = 0;
  /** ENTER samples waiting for their EXIT, by nonce (InvariantCheckProcessor's call_map). */
  static final Map<Integer, EnterCall> openCalls = new LinkedHashMap<>();

  /** Where the sample being checked was read from (an ENTER is checked later than it is read). */
  static int curLine = 0;

  static String curFile = null;

  public static void main(String[] args) throws Exception {
    String invFile = null;
    String outFile = null;
    String summaryFile = null;
    List<String> dtraces = new ArrayList<>();
    List<String> configOptions = new ArrayList<>();
    for (int i = 0; i < args.length; i++) {
      switch (args[i]) {
        case "--inv":
          invFile = args[++i];
          break;
        case "--out":
          outFile = args[++i];
          break;
        case "--summary":
          summaryFile = args[++i];
          break;
        case "--max-violations":
          maxViolations = Integer.parseInt(args[++i]);
          break;
        case "--config_option":
          configOptions.add(args[++i]);
          break;
        default:
          if (args[i].startsWith("--")) {
            throw new IllegalArgumentException("unknown option " + args[i]);
          }
          dtraces.add(args[i]);
      }
    }
    if (invFile == null || outFile == null || summaryFile == null || dtraces.isEmpty()) {
      throw new IllegalArgumentException(
          "usage: --inv F --out F --summary F [--max-violations N] [--config_option k=v]... DTRACE...");
    }
    daikon.LogHelper.setupLogs(java.util.logging.Level.INFO);
    for (String opt : configOptions) {
      daikon.config.Configuration.getInstance().apply(opt);
    }

    // Same set-up as PrintInvariants, so printed text and filter decisions match its output.
    PptMap ppts = FileIO.read_serialized_pptmap(new File(invFile), true);
    Daikon.setup_proto_invs();
    Daikon.setup_NISuppression();
    PrintInvariants.validateGuardNulls();

    // Text of every invariant, computed before any sample is read (check() does not mutate, but
    // this keeps the text independent of the data anyway).
    Map<Invariant, String[]> text = new IdentityHashMap<>();
    InvariantFilters fi = InvariantFilters.defaultFilters();
    for (PptTopLevel ppt : ppts.pptIterable()) {
      for (PptSlice slice : ppt.views_iterable()) {
        for (Invariant inv : slice.invs) {
          if (inv instanceof Equality) {
            continue;
          }
          stats.put(inv, new Stats());
          String printed = null;
          String err = null;
          String kept = null;
          try {
            Invariant guarded = inv.createGuardedInvariant(false);
            StringWriter sw = new StringWriter();
            PrintWriter pw = new PrintWriter(sw);
            PrintInvariants.print_invariant(guarded != null ? guarded : inv, pw, 0, ppt);
            pw.flush();
            printed = sw.toString().trim();
          } catch (Throwable t) {
            err = String.valueOf(t);
          }
          try {
            kept = String.valueOf(fi.shouldKeep(inv) == null);
          } catch (Throwable t) {
            kept = "error: " + t;
          }
          String fmt;
          try {
            fmt = inv.format();
          } catch (Throwable t) {
            fmt = null;
          }
          text.put(inv, new String[] {printed, err, kept, fmt});
        }
      }
    }

    FileIO.read_data_trace_files(dtraces, ppts, new Processor(), false);

    // Diagnostics: ENTER samples of calls that never returned. Not used by Daikon's inference,
    // so they never count toward the eligible evaluations/violations.
    long unmatchedEnters = openCalls.size();
    for (EnterCall ec : openCalls.values()) {
      curLine = ec.linenum;
      curFile = ec.file;
      add(ec.ppt, ec.vt, ppts, new HashSet<>(), false, true);
    }

    File out = new File(outFile);
    File tmp = new File(out.getParentFile(), "." + out.getName() + ".tmp");
    try (Writer w =
        new BufferedWriter(
            new OutputStreamWriter(new FileOutputStream(tmp), StandardCharsets.UTF_8))) {
      for (PptTopLevel ppt : ppts.pptIterable()) {
        for (PptSlice slice : ppt.views_iterable()) {
          for (Invariant inv : slice.invs) {
            Stats s = stats.get(inv);
            if (s == null) {
              continue;
            }
            String[] t = text.get(inv);
            StringBuilder sb = new StringBuilder();
            sb.append('{');
            kv(sb, "ppt", ppt.name()).append(',');
            kv(sb, "printed", t[0]).append(',');
            kv(sb, "print_error", t[1]).append(',');
            sb.append("\"filter_keep\":")
                .append("true".equals(t[2]) ? "true" : "false".equals(t[2]) ? "false" : "null")
                .append(',');
            kv(sb, "format", t[3]).append(',');
            kv(sb, "class", inv.getClass().getName()).append(',');
            sb.append("\"vars\":[");
            for (int i = 0; i < slice.var_infos.length; i++) {
              if (i > 0) {
                sb.append(',');
              }
              str(sb, slice.var_infos[i].name());
            }
            sb.append("],");
            sb.append("\"active\":").append(inv.isActive()).append(',');
            num(sb, "evaluations", s.evaluations);
            num(sb, "violations", s.violationsDirect + s.violationsPropagated);
            num(sb, "violations_direct", s.violationsDirect);
            num(sb, "violations_propagated", s.violationsPropagated);
            num(sb, "violations_all_nan", s.violationsAllNaN);
            num(sb, "skipped_missing", s.skippedMissing);
            num(sb, "skipped_out_of_bounds", s.skippedOutOfBounds);
            list(sb, "first_violations", s.firstViolations).append(',');
            num(sb, "diag_evaluations_unmatched_entry", s.diagEvaluations);
            num(sb, "diag_violations_unmatched_entry", s.diagViolationsUnmatchedEntry);
            num(
                sb,
                "diag_violations_propagated_unmatched_entry",
                s.diagViolationsPropagatedUnmatchedEntry);
            list(sb, "first_diag_violations", s.firstDiagViolations);
            sb.append("}\n");
            w.write(sb.toString());
          }
        }
      }
    }
    if (!tmp.renameTo(out)) {
      throw new RuntimeException("could not rename " + tmp + " to " + out);
    }

    File sum = new File(summaryFile);
    File stmp = new File(sum.getParentFile(), "." + sum.getName() + ".tmp");
    try (Writer w =
        new BufferedWriter(
            new OutputStreamWriter(new FileOutputStream(stmp), StandardCharsets.UTF_8))) {
      w.write(
          String.format(
              "{\"samples_read\":%d,\"enter_samples\":%d,\"enter_without_exit\":%d,"
                  + "\"exit_without_enter\":%d,\"propagated_samples\":%d,"
                  + "\"unmapped_parent_var_values\":%d,\"invariants\":%d}%n",
              samplesRead,
              enterSamples,
              unmatchedEnters,
              exitWithoutEnter,
              propagatedSamples,
              unmappedParentVars,
              stats.size()));
    }
    if (!stmp.renameTo(sum)) {
      throw new RuntimeException("could not rename " + stmp + " to " + sum);
    }
    System.out.printf(
        "DaikonCandidateChecker: %d samples, %d invariants, %d with eligible violations,"
            + " %d unmatched ENTER samples (diagnostic only)%n",
        samplesRead,
        stats.size(),
        stats.values().stream()
            .filter(x -> x.violationsDirect + x.violationsPropagated > 0)
            .count(),
        unmatchedEnters);
  }

  static final class Processor extends FileIO.Processor {
    @Override
    public void process_sample(PptMap all_ppts, PptTopLevel ppt, ValueTuple vt, Integer nonce) {
      samplesRead++;
      // As InvariantCheckProcessor: add orig and derived variables, then intern.
      FileIO.compute_orig_variables(ppt, vt.vals, vt.mods, nonce);
      FileIO.compute_derived_variables(ppt, vt.vals, vt.mods);
      vt = new ValueTuple(vt.vals, vt.mods);
      int line = FileIO.get_linenum();
      String file = FileIO.data_trace_state == null ? null : FileIO.data_trace_state.filename;

      // As InvariantCheckProcessor: an ENTER sample waits for its call's EXIT.
      if (ppt.ppt_name.isEnterPoint()) {
        enterSamples++;
        if (nonce != null) {
          openCalls.put(nonce, new EnterCall(ppt, vt, line, file));
        }
        return;
      }
      if (ppt.ppt_name.isExitPoint()) {
        EnterCall ec = nonce == null ? null : openCalls.remove(nonce);
        if (ec == null) {
          exitWithoutEnter++; // as InvariantCheckProcessor: skipped
          return;
        }
        curLine = ec.linenum;
        curFile = ec.file;
        add(ec.ppt, ec.vt, all_ppts, new HashSet<>(), false, false);
      }
      curLine = line;
      curFile = file;
      add(ppt, vt, all_ppts, new HashSet<>(), false, false);
    }
  }

  static void add(
      PptTopLevel ppt,
      ValueTuple vt,
      PptMap all_ppts,
      Set<PptTopLevel> visited,
      boolean propagated,
      boolean diagnostic) {
    if (!visited.add(ppt)) {
      return;
    }
    // As InvariantCheckProcessor: a numbered exit is also applied to the combined exit.
    // (Splitting is disabled, so there are no PptConditional points.)
    if (ppt.ppt_name.isNumberedExitPoint()) {
      PptTopLevel parent = all_ppts.get(ppt.ppt_name.makeExit());
      if (parent != null) {
        parent.get_missingOutOfBounds(ppt, vt);
        add(parent, vt, all_ppts, visited, propagated, diagnostic);
      }
    }

    if (ppt.var_infos.length > 0) {
      slice_loop:
      for (PptSlice slice : ppt.views_iterable()) {
        boolean missing = false;
        boolean outOfBounds = false;
        for (VarInfo v : slice.var_infos) {
          missing |= v.isMissing(vt);
          outOfBounds |= v.missingOutOfBounds();
        }
        if (missing || outOfBounds) {
          if (!diagnostic) {
            for (Invariant inv : slice.invs) {
              Stats s = stats.get(inv);
              if (s != null) {
                if (missing) {
                  s.skippedMissing++;
                } else {
                  s.skippedOutOfBounds++;
                }
              }
            }
          }
          continue slice_loop;
        }
        for (Invariant inv : slice.invs) {
          Stats s = stats.get(inv);
          if (s == null || !inv.isActive()) {
            continue;
          }
          InvariantStatus status = check(inv, slice, vt);
          boolean violated = status != InvariantStatus.NO_CHANGE;
          if (diagnostic) {
            s.diagEvaluations++;
            if (violated) {
              if (propagated) {
                s.diagViolationsPropagatedUnmatchedEntry++;
              } else {
                s.diagViolationsUnmatchedEntry++;
              }
              if (s.firstDiagViolations.size() < maxViolations) {
                s.firstDiagViolations.add(
                    violation(
                        slice,
                        vt,
                        status,
                        propagated ? "propagated_unmatched_entry" : "unmatched_entry"));
              }
            }
          } else {
            s.evaluations++;
            if (violated) {
              if (allNaN(slice, vt)) {
                s.violationsAllNaN++;
              }
              if (propagated) {
                s.violationsPropagated++;
              } else {
                s.violationsDirect++;
              }
              if (s.firstViolations.size() < maxViolations) {
                s.firstViolations.add(
                    violation(slice, vt, status, propagated ? "propagated" : "direct"));
              }
            }
          }
        }
      }
    }

    // OBJECT/CLASS: follow the PARENT/USER relations of the hierarchy. ENTER_EXIT (orig vars) and
    // EXIT_EXITNN (handled above) are not sample flows to another point's invariants.
    for (PptRelation rel : ppt.parents) {
      PptRelation.PptRelationType type = rel.getRelationType();
      if (type != PptRelation.PptRelationType.PARENT && type != PptRelation.PptRelationType.USER) {
        continue;
      }
      PptTopLevel parent = rel.parent;
      int n = parent.var_infos.length - parent.num_static_constant_vars;
      Object[] vals = new Object[n];
      int[] mods = new int[n];
      for (VarInfo pv : parent.var_infos) {
        if (pv.is_static_constant || pv.isDerived()) {
          continue;
        }
        VarInfo cv = rel.childVar(pv);
        if (cv == null) {
          unmappedParentVars++;
          vals[pv.value_index] = null;
          mods[pv.value_index] = ValueTuple.MISSING_FLOW;
        } else {
          vals[pv.value_index] = vt.getValueOrNull(cv);
          mods[pv.value_index] = vt.getModified(cv);
        }
      }
      FileIO.compute_derived_variables(parent, vals, mods);
      propagatedSamples++;
      add(parent, new ValueTuple(vals, mods), all_ppts, visited, true, diagnostic);
    }
  }

  /** Invariant.add_sample's dispatch, with the non-mutating check methods. */
  static InvariantStatus check(Invariant inv, PptSlice slice, ValueTuple vt) {
    if (slice instanceof PptSlice1) {
      VarInfo v = slice.var_infos[0];
      return ((UnaryInvariant) inv).check(vt.getValue(v), vt.getModified(v), 1);
    } else if (slice instanceof PptSlice2) {
      VarInfo v1 = slice.var_infos[0];
      VarInfo v2 = slice.var_infos[1];
      return ((BinaryInvariant) inv)
          .check_unordered(vt.getValue(v1), vt.getValue(v2), vt.getModified(v1), 1);
    } else {
      VarInfo v1 = slice.var_infos[0];
      VarInfo v2 = slice.var_infos[1];
      VarInfo v3 = slice.var_infos[2];
      return ((TernaryInvariant) inv)
          .check(vt.getValue(v1), vt.getValue(v2), vt.getValue(v3), vt.getModified(v1), 1);
    }
  }

  /**
   * True if every value of the slice's variables in this sample is NaN (a double, or a non-empty
   * double[] of only NaNs). Diagnostic only: such a violation is still a violation.
   */
  static boolean allNaN(PptSlice slice, ValueTuple vt) {
    for (VarInfo v : slice.var_infos) {
      Object val = vt.getValueOrNull(v);
      if (val instanceof Double) {
        if (!((Double) val).isNaN()) {
          return false;
        }
      } else if (val instanceof double[] && ((double[]) val).length > 0) {
        for (double d : (double[]) val) {
          if (!Double.isNaN(d)) {
            return false;
          }
        }
      } else {
        return false;
      }
    }
    return true;
  }

  static String violation(PptSlice slice, ValueTuple vt, InvariantStatus status, String origin) {
    StringBuilder sb = new StringBuilder("{\"values\":{");
    for (int i = 0; i < slice.var_infos.length; i++) {
      if (i > 0) {
        sb.append(',');
      }
      VarInfo v = slice.var_infos[i];
      str(sb, v.name()).append(':');
      str(sb, show(vt.getValueOrNull(v)));
    }
    sb.append("},");
    kv(sb, "status", status.toString()).append(',');
    kv(sb, "origin", origin).append(',');
    sb.append("\"line\":").append(curLine).append(',');
    kv(sb, "file", curFile);
    return sb.append('}').toString();
  }

  static String show(Object val) {
    if (val == null) {
      return "null";
    } else if (val instanceof long[]) {
      return Arrays.toString((long[]) val);
    } else if (val instanceof double[]) {
      return Arrays.toString((double[]) val);
    } else if (val instanceof String[]) {
      return Arrays.toString((String[]) val);
    } else if (val instanceof String) {
      return "\"" + val + "\"";
    }
    return String.valueOf(val);
  }

  static void num(StringBuilder sb, String k, long v) {
    str(sb, k).append(':').append(v).append(',');
  }

  static StringBuilder list(StringBuilder sb, String k, List<String> items) {
    str(sb, k).append(":[");
    for (int i = 0; i < items.size(); i++) {
      if (i > 0) {
        sb.append(',');
      }
      sb.append(items.get(i));
    }
    return sb.append(']');
  }

  static StringBuilder kv(StringBuilder sb, String k, String v) {
    str(sb, k).append(':');
    if (v == null) {
      return sb.append("null");
    }
    return str(sb, v);
  }

  static StringBuilder str(StringBuilder sb, String s) {
    sb.append('"');
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '"':
          sb.append("\\\"");
          break;
        case '\\':
          sb.append("\\\\");
          break;
        case '\n':
          sb.append("\\n");
          break;
        case '\r':
          sb.append("\\r");
          break;
        case '\t':
          sb.append("\\t");
          break;
        default:
          if (c < 0x20) {
            sb.append(String.format("\\u%04x", (int) c));
          } else {
            sb.append(c);
          }
      }
    }
    return sb.append('"');
  }
}
