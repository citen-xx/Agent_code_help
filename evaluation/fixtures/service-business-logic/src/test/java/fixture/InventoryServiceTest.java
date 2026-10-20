package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class InventoryServiceTest {
    private final InventoryService service = new InventoryService();

    @Test
    public void reservesRequestedQuantity() {
        assertEquals(7, service.reserve(10, 3));
    }

    @Test
    public void allowsExactInventory() {
        assertEquals(0, service.reserve(3, 3));
    }

    @Test(expected = IllegalArgumentException.class)
    public void rejectsInsufficientInventory() {
        service.reserve(2, 3);
    }
}
