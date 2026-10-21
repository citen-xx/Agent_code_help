package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CustomerServiceTest {
    @Test
    public void missingAddressGetsDefault() {
        CustomerDto dto = new CustomerService(new CustomerRepository(), new AddressRepository()).load(2);
        assertEquals("Unknown", dto.name);
        assertEquals("Unknown", dto.city);
    }
}
