package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class PriceServiceTest {
    @Test
    public void premiumDiscountApplies() {
        assertEquals(90, new PriceService().quote(100));
    }
}
