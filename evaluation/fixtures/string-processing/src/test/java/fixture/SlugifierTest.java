package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class SlugifierTest {
    private final Slugifier slugifier = new Slugifier();

    @Test
    public void normalizesSimpleTitle() {
        assertEquals("hello-world", slugifier.slug(" Hello World "));
    }

    @Test
    public void normalizesAlreadySeparatedText() {
        assertEquals("order-service", slugifier.slug("Order-Service"));
    }
}
