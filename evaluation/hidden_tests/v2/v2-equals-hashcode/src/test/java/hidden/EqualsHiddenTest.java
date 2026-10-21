package hidden;

import fixture.CustomerKey;
import org.junit.Assert;
import org.junit.Test;

public class EqualsHiddenTest {
    @Test
    public void equalKeysShareHashCode() {
        Assert.assertEquals(
            new CustomerKey(3, "A").hashCode(),
            new CustomerKey(3, "A").hashCode()
        );
    }

    @Test
    public void differentIdsAreNotEqual() {
        Assert.assertNotEquals(new CustomerKey(1, "A"), new CustomerKey(2, "A"));
    }
}
