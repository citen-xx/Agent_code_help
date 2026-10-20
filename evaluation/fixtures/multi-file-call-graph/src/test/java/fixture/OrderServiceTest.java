package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class OrderServiceTest {
    @Test
    public void calculatesDoublePrice() {
        assertEquals(6, new OrderService().total(3));
    }
}
