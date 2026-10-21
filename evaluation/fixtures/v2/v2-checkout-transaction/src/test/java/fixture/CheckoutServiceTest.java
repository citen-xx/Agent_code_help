package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CheckoutServiceTest {
    @Test
    public void reservesStockAndSavesOrder() {
        OrderRepository orders = new OrderRepository();
        int remaining = new CheckoutService(new InventoryRepository(), orders)
            .checkout("o1", 3);
        assertEquals(7, remaining);
        assertEquals(1, orders.savedCount());
    }
}
