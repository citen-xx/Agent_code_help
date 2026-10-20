package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class TextNormalizerTest {
    private final TextNormalizer normalizer = new TextNormalizer();

    @Test
    public void trimsOuterWhitespace() {
        assertEquals("Hello", normalizer.normalize("  Hello  "));
    }

    @Test
    public void preservesCamelCase() {
        assertEquals("OrderId", normalizer.normalize("OrderId"));
    }
}
