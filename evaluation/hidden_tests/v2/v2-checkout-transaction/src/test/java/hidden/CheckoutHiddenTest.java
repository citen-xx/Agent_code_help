package hidden;

import fixture.CheckoutService;
import fixture.InventoryRepository;
import fixture.OrderRepository;
import org.junit.Test;

public class CheckoutHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void rejectsOrderAboveStock() {
        new CheckoutService(new InventoryRepository(), new OrderRepository())
            .checkout("o2", 11);
    }
}
