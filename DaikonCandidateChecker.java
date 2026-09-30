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
 * sample handling (orig/derived variables, numbered EXITnn samples also applied to the combined
 * EXIT with get_missingOutOfBounds, slices skipped when a variable is missing or out of bounds,
 * inactive invariants skipped) but uses the non-mutating {@code check} methods, so the invariants
 * stay exactly as inferred and every violation is counted. Differences from the stock checker, by
 * design:
 *
 * <ul>
 *   <li>ENTER samples are checked when read (as Daikon's own inference uses them), not deferred to
 *       the matching EXIT; the stock checker never checks the ENTER sample of a call that throws.
 *   <li>Samples are also propagated along PARENT/USER relations of the ppt hierarchy (method ENTER
 *       / combined EXIT to OBJECT, OBJECT to CLASS), which is how Daikon's OBJECT/CLASS invariants
 *       are justified. The stock checker never evaluates OBJECT/CLASS invariants.
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

  static final class Stats {
    long evaluations = 0;
    long violations = 0;
    long violationsDirect = 0; // at this ppt, or EXITnn -> combined EXIT (the stock checker's path)
    long violationsPropagated = 0; // reached via a PARENT/USER relation (OBJECT/CLASS)
    long skippedMissing = 0;
    List<String> firstViolations = new ArrayList<>();
  }

  static final IdentityHashMap<Invariant, Stats> stats = new IdentityHashMap<>();
  static int maxViolations = 5;
  static long samplesRead = 0;
  static long enterSamples = 0;
  static long exitWithoutEnter = 0;
  static long propagatedSamples = 0;
  static long unmappedParentVars = 0;
  static final Set<Integer> openCalls = new HashSet<>();

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
            sb.append("\"evaluations\":").append(s.evaluations).append(',');
            sb.append("\"violations\":").append(s.violations).append(',');
            sb.append("\"violations_direct\":").append(s.violationsDirect).append(',');
            sb.append("\"violations_propagated\":").append(s.violationsPropagated).append(',');
            sb.append("\"skipped_missing\":").append(s.skippedMissing).append(',');
            sb.append("\"first_violations\":[");
            for (int i = 0; i < s.firstViolations.size(); i++) {
              if (i > 0) {
                sb.append(',');
              }
              sb.append(s.firstViolations.get(i));
            }
            sb.append("]}\n");
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
              openCalls.size(),
              exitWithoutEnter,
              propagatedSamples,
              unmappedParentVars,
              stats.size()));
    }
    if (!stmp.renameTo(sum)) {
      throw new RuntimeException("could not rename " + stmp + " to " + sum);
    }
    System.out.printf(
        "DaikonCandidateChecker: %d samples, %d invariants, %d with violations%n",
        samplesRead, stats.size(), stats.values().stream().filter(x -> x.violations > 0).count());
  }

  static final class Processor extends FileIO.Processor {
    @Override
    public void process_sample(
        PptMap all_ppts, PptTopLevel ppt, ValueTuple vt, Integer nonce) {
      samplesRead++;
      // As InvariantCheckProcessor: add orig and derived variables, then intern.
      FileIO.compute_orig_variables(ppt, vt.vals, vt.mods, nonce);
      FileIO.compute_derived_variables(ppt, vt.vals, vt.mods);
      vt = new ValueTuple(vt.vals, vt.mods);

      if (ppt.ppt_name.isEnterPoint()) {
        enterSamples++;
        if (nonce != null) {
          openCalls.add(nonce);
        }
      } else if (ppt.ppt_name.isExitPoint()) {
        // As InvariantCheckProcessor: an exit whose enter was never seen is skipped.
        if (nonce == null || !openCalls.remove(nonce)) {
          exitWithoutEnter++;
          return;
        }
      }
      add(ppt, vt, all_ppts, new HashSet<>(), false);
    }
  }

  static void add(
      PptTopLevel ppt, ValueTuple vt, PptMap all_ppts, Set<PptTopLevel> visited, boolean propagated) {
    if (!visited.add(ppt)) {
      return;
    }
    // As InvariantCheckProcessor: a numbered exit is also applied to the combined exit.
    // (Splitting is disabled, so there are no PptConditional points.)
    if (ppt.ppt_name.isNumberedExitPoint()) {
      PptTopLevel parent = all_ppts.get(ppt.ppt_name.makeExit());
      if (parent != null) {
        parent.get_missingOutOfBounds(ppt, vt);
        add(parent, vt, all_ppts, visited, propagated);
      }
    }

    if (ppt.var_infos.length > 0) {
      slice_loop:
      for (PptSlice slice : ppt.views_iterable()) {
        for (VarInfo v : slice.var_infos) {
          if (v.isMissing(vt) || v.missingOutOfBounds()) {
            for (Invariant inv : slice.invs) {
              Stats s = stats.get(inv);
              if (s != null) {
                s.skippedMissing++;
              }
            }
            continue slice_loop;
          }
        }
        for (Invariant inv : slice.invs) {
          Stats s = stats.get(inv);
          if (s == null || !inv.isActive()) {
            continue;
          }
          InvariantStatus status = check(inv, slice, vt);
          s.evaluations++;
          if (status != InvariantStatus.NO_CHANGE) {
            s.violations++;
            if (propagated) {
              s.violationsPropagated++;
            } else {
              s.violationsDirect++;
            }
            if (s.firstViolations.size() < maxViolations) {
              s.firstViolations.add(violation(ppt, slice, vt, status, propagated));
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
      add(parent, new ValueTuple(vals, mods), all_ppts, visited, true);
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

  static String violation(
      PptTopLevel ppt, PptSlice slice, ValueTuple vt, InvariantStatus status, boolean propagated) {
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
    sb.append("\"propagated\":").append(propagated).append(',');
    sb.append("\"line\":").append(FileIO.get_linenum()).append(',');
    kv(sb, "file", FileIO.data_trace_state == null ? null : FileIO.data_trace_state.filename);
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
