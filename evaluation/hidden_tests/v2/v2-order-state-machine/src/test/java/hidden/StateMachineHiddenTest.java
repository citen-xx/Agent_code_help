package hidden;

import fixture.OrderEvent;
import fixture.OrderService;
import fixture.OrderState;
import org.junit.Test;

public class StateMachineHiddenTest {
    @Test(expected = IllegalStateException.class)
    public void shippedOrderCannotBeCancelled() {
        new OrderService().apply(OrderState.SHIPPED, OrderEvent.CANCEL);
    }

    @Test(expected = IllegalStateException.class)
    public void paidOrderCannotBePaidAgain() {
        new OrderService().apply(OrderState.PAID, OrderEvent.PAY);
    }
}
