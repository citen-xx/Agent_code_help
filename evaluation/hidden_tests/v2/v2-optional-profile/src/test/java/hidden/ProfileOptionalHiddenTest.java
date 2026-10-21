package hidden;

import fixture.CustomerRepository;
import fixture.ProfileService;
import org.junit.Assert;
import org.junit.Test;

public class ProfileOptionalHiddenTest {
    @Test
    public void existingCustomerIsNormalized() {
        Assert.assertEquals(
            "ALICE", new ProfileService(new CustomerRepository()).displayName(1)
        );
    }
}
