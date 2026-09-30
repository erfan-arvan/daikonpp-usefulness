/**
 * Test-boundary markers for Chicory traces. DaikonMarkedTestRunner calls begin(id) before and
 * end(id, threadsStarted) after every test, and prints "[DaikonMarkedTestRunner] MARK id
 * class::method" so ids map to test names. With "^DaikonTestMarker\." added to Chicory's
 * --ppt-select-pattern, the two ENTER records delimit each test's samples in the trace.
 *
 * <p>Only primitive parameters: the pipeline's --ppt-omit-pattern contains an unanchored "java\.",
 * which Chicory also matches against parameter types, so a String parameter would get the markers
 * omitted. threadsStarted is the number of JVM threads started while the test ran; a nonzero value
 * makes the window's attribution ambiguous. The methods do nothing: only their records matter.
 */
public final class DaikonTestMarker {
  private DaikonTestMarker() {}

  public static void begin(int testId) {}

  public static void end(int testId, long threadsStarted) {}
}
