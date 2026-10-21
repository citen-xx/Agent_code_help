package fixture;

import org.junit.Test;

public class OrderServiceTest {
    @Test(expected = InvalidOrderException.class)
    public void rejectsInvalidOrder() {
        new OrderService().submit(0);
    }
}
