package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CalculatorTest {
    @Test
    public void addsPositiveNumbers() {
        assertEquals(5, new Calculator().add(2, 3));
    }

    @Test
    public void addsZero() {
        assertEquals(0, new Calculator().add(0, 0));
    }
}
