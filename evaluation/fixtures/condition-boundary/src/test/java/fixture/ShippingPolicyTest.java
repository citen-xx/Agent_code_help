package fixture;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import org.junit.Test;

public class ShippingPolicyTest {
    private final ShippingPolicy policy = new ShippingPolicy();

    @Test
    public void largeOrderIsFree() {
        assertTrue(policy.freeShipping(120, false));
    }

    @Test
    public void smallNonPremiumOrderIsNotFree() {
        assertFalse(policy.freeShipping(99, false));
    }

    @Test
    public void premiumOrderIsFree() {
        assertTrue(policy.freeShipping(50, true));
    }
}
