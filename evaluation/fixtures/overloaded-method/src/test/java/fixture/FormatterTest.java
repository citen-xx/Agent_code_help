package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class FormatterTest {
    private final Formatter formatter = new Formatter();

    @Test
    public void keepsStringValue() {
        assertEquals("42", formatter.format("42"));
    }

    @Test
    public void keepsIntegerValue() {
        assertEquals(42, formatter.format(42));
    }
}
