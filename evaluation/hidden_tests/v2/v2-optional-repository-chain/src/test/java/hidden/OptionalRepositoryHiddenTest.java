package hidden;

import fixture.AddressRepository;
import fixture.CustomerRepository;
import fixture.CustomerService;
import org.junit.Assert;
import org.junit.Test;

public class OptionalRepositoryHiddenTest {
    @Test
    public void existingCustomerIsMapped() {
        Assert.assertEquals(
            "Shanghai",
            new CustomerService(new CustomerRepository(), new AddressRepository()).load(1).city
        );
    }
}
