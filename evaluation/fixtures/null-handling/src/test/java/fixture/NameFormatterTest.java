package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class NameFormatterTest {
    private final NameFormatter formatter = new NameFormatter();

    @Test
    public void nullBecomesGuest() {
        assertEquals("Guest", formatter.displayName(null));
    }

    @Test
    public void trimsName() {
        assertEquals("Alice", formatter.displayName(" Alice "));
    }

    @Test
    public void blankBecomesGuest() {
        assertEquals("Guest", formatter.displayName(""));
    }
}
