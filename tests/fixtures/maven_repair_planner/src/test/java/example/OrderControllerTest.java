package example;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class OrderControllerTest {
    @Test
    public void createsOrderPrice() {
        OrderController controller = new OrderController();
        assertEquals(6, controller.create(3));
    }
}
