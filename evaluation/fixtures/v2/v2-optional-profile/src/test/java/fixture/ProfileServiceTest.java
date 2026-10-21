package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class ProfileServiceTest {
    @Test
    public void missingCustomerGetsFallback() {
        assertEquals("GUEST", new ProfileService(new CustomerRepository()).displayName(9));
    }
}
