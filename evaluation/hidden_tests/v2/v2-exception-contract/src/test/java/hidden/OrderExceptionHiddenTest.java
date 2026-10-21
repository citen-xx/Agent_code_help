package hidden;

import fixture.InvalidOrderException;
import fixture.OrderService;
import org.junit.Test;

public class OrderExceptionHiddenTest {
    @Test
    public void acceptsValidOrder() {
        org.junit.Assert.assertTrue(new OrderService().submit(1));
    }

    @Test(expected = InvalidOrderException.class)
    public void rejectsNegativeOrder() {
        new OrderService().submit(-3);
    }
}
