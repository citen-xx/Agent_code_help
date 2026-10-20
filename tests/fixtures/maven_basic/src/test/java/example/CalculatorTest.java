package example;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class CalculatorTest {
    @Test
    public void addsTwoNumbers() {
        assertEquals(4, Calculator.add(2, 2));
    }
}
