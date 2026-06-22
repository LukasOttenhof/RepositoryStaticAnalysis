import java.util.*;
import java.io.*;

public class test {

    // Hardcoded credentials (security hotspot)
    private static final String PASSWORD = "admin123";
    private static final String DB_URL = "jdbc:mysql://localhost/mydb";

    public static void main(String[] args) {
        test t = new test();
        System.out.println(t.divide(10, 0));   // potential ArithmeticException
        t.unusedMethod();
        String result = t.riskyMethod();
    }

    // Missing null check, unreachable code
    public int divide(int a, int b) {
        return a / b;
    }

    // Method too complex, too many branches (cognitive complexity issue)
    public String classify(int n) {
        if (n > 0) {
            if (n > 100) {
                if (n > 1000) {
                    if (n > 10000) {
                        return "very large";
                    } else {
                        return "large";
                    }
                } else {
                    return "medium";
                }
            } else {
                if (n > 50) {
                    return "small-medium";
                } else {
                    return "small";
                }
            }
        } else if (n < 0) {
            if (n < -100) {
                return "very negative";
            } else {
                return "negative";
            }
        } else {
            return "zero";
        }
    }

    // Resource leak — stream never closed
    public void readFile(String path) throws IOException {
        FileInputStream fis = new FileInputStream(path);
        int data = fis.read();
        System.out.println(data);
        // fis.close() never called
    }

    // Empty catch block (bad practice)
    public void silentException() {
        try {
            int[] arr = new int[5];
            arr[10] = 1;
        } catch (ArrayIndexOutOfBoundsException e) {
            // swallowed
        }
    }

    // Duplicate code block
    public int sumA(int[] nums) {
        int total = 0;
        for (int i = 0; i < nums.length; i++) {
            total += nums[i];
        }
        return total;
    }

    public int sumB(int[] nums) {
        int total = 0;
        for (int i = 0; i < nums.length; i++) {
            total += nums[i];
        }
        return total;
    }

    // Method that modifies a parameter (confusing)
    public int[] modifyInput(int[] data) {
        data[0] = 999;
        return data;
    }

    // Unused method
    private void unusedMethod() {
        int x = 42;  // unused local variable
    }

    // String comparison with == instead of .equals()
    public boolean compareStrings(String a, String b) {
        return a == b;
    }

    // NullPointerException risk
    public String riskyMethod() {
        String s = null;
        return s.toUpperCase();
    }

    // Magic numbers
    public double circleArea(double r) {
        return 3.14159 * r * r;
    }
}
