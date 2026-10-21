package fixture;

import static org.junit.Assert.assertEquals;
import java.util.HashSet;
import java.util.Set;
import org.junit.Test;

public class CustomerKeyTest {
    @Test
    public void duplicateKeysCollapse() {
        Set<CustomerKey> keys = new HashSet<>();
        keys.add(new CustomerKey(1, "Alice"));
        keys.add(new CustomerKey(1, "Alicia"));
        assertEquals(1, keys.size());
    }
}
