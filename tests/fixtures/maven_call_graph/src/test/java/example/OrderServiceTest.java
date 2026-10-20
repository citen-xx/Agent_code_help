package example;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public class OrderServiceTest {
    @Test
    public void doublesOrderPrice() {
        OrderService service = new OrderService();
        assertEquals(6, service.create(3));
    }
}
