import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import junit.framework.TestCase;
import junit.framework.TestResult;
import org.junit.runner.Description;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;
import org.junit.runner.manipulation.Filter;

/**
 * JUnit runner used to drive Daikon's Chicory front end over a chosen subset of tests, so we can
 * produce one trace for "the suite minus the bug-revealing test(s)" and a second trace for "just
 * the bug-revealing test(s)", without touching any source file.
 *
 * <p>Handles both JUnit4 (anything not extending {@code junit.framework.TestCase}, run via {@link
 * Request}/{@link Filter}) and JUnit3 (anything extending {@code TestCase}, run via reflection +
 * {@link TestResult}, since JUnit4's {@code Request} API does not drive legacy TestCase classes).
 * Test failures are ignored either way (we only care about the execution trace Chicory records),
 * a summary line is printed at the end so a run log can sanity-check the counts.
 *
 * <p>Each CLI argument is one of:
 *
 * <ul>
 *   <li>{@code pkg.ClassName} — run every test in the class.
 *   <li>{@code pkg.ClassName::methodName} — run only that one test method.
 *   <li>{@code pkg.ClassName::!m1,!m2} — run every test in the class EXCEPT m1 and m2.
 * </ul>
 */
public class DaikonTestRunner {

  static final class ExcludeMethodsFilter extends Filter {
    private final Set<String> excluded;

    ExcludeMethodsFilter(Set<String> excluded) {
      this.excluded = excluded;
    }

    @Override
    public boolean shouldRun(Description description) {
      if (description.isTest()) {
        return !excluded.contains(description.getMethodName());
      }
      return true;
    }

    @Override
    public String describe() {
      return "ExcludeMethods" + excluded;
    }
  }

  private static boolean isJUnit3(Class<?> cls) {
    return TestCase.class.isAssignableFrom(cls);
  }

  /** All public no-arg {@code testXxx()} methods declared anywhere in the class hierarchy. */
  private static List<String> junit3TestMethodNames(Class<?> cls) {
    List<String> names = new ArrayList<>();
    Set<String> seen = new HashSet<>();
    for (Class<?> c = cls; c != null && c != Object.class; c = c.getSuperclass()) {
      for (Method m : c.getDeclaredMethods()) {
        if (Modifier.isPublic(m.getModifiers())
            && m.getParameterCount() == 0
            && m.getName().startsWith("test")
            && seen.add(m.getName())) {
          names.add(m.getName());
        }
      }
    }
    return names;
  }

  /** Instantiates a JUnit3 TestCase configured to run the named test method. */
  private static TestCase newJUnit3Instance(Class<?> cls, String methodName) throws Exception {
    try {
      Constructor<?> ctor = cls.getConstructor(String.class);
      return (TestCase) ctor.newInstance(methodName);
    } catch (NoSuchMethodException e) {
      TestCase tc = (TestCase) cls.getDeclaredConstructor().newInstance();
      tc.setName(methodName);
      return tc;
    }
  }

  private static int[] runJUnit3(Class<?> cls, Set<String> excluded, String onlyMethod) {
    int total = 0;
    int failures = 0;
    List<String> methods =
        onlyMethod != null ? List.of(onlyMethod) : junit3TestMethodNames(cls);
    for (String m : methods) {
      if (excluded.contains(m)) continue;
      try {
        TestCase tc = newJUnit3Instance(cls, m);
        TestResult result = new TestResult();
        tc.run(result);
        total++;
        if (!result.wasSuccessful()) failures++;
      } catch (Throwable t) {
        System.err.println("[DaikonTestRunner] error running JUnit3 " + cls.getName() + "::" + m + ": " + t);
        total++;
        failures++;
      }
    }
    return new int[] {total, failures};
  }

  public static void main(String[] args) {
    JUnitCore core = new JUnitCore();
    int total = 0;
    int failures = 0;

    for (String spec : args) {
      try {
        int sep = spec.indexOf("::");
        String className = sep >= 0 ? spec.substring(0, sep) : spec;
        String rest = sep >= 0 ? spec.substring(sep + 2) : null;
        Class<?> cls = Class.forName(className);

        if (isJUnit3(cls)) {
          Set<String> excl = new HashSet<>();
          String onlyMethod = null;
          if (rest != null) {
            if (rest.startsWith("!")) {
              for (String part : rest.split(",")) {
                part = part.trim();
                if (part.startsWith("!")) part = part.substring(1);
                if (!part.isEmpty()) excl.add(part);
              }
            } else {
              onlyMethod = rest;
            }
          }
          int[] r = runJUnit3(cls, excl, onlyMethod);
          total += r[0];
          failures += r[1];
          continue;
        }

        // JUnit4 path.
        Request req;
        if (rest != null) {
          if (rest.startsWith("!")) {
            Set<String> excl = new HashSet<>();
            for (String part : rest.split(",")) {
              part = part.trim();
              if (part.startsWith("!")) part = part.substring(1);
              if (!part.isEmpty()) excl.add(part);
            }
            req = Request.aClass(cls).filterWith(new ExcludeMethodsFilter(excl));
          } else {
            req = Request.method(cls, rest);
          }
        } else {
          req = Request.aClass(cls);
        }

        Result r = core.run(req);
        total += r.getRunCount();
        failures += r.getFailureCount();
      } catch (Throwable t) {
        System.err.println("[DaikonTestRunner] error running '" + spec + "': " + t);
      }
    }

    System.out.println("[DaikonTestRunner] total=" + total + " failures=" + failures);
  }
}
