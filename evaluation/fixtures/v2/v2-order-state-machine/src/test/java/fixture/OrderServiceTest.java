package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class OrderServiceTest {
    @Test
    public void paidTransition() {
        assertEquals(
            OrderState.PAID,
            new OrderService().apply(OrderState.CREATED, OrderEvent.PAY)
        );
    }
}
