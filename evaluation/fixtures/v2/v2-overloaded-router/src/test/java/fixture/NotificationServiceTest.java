package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class NotificationServiceTest {
    @Test
    public void numericOverloadUsesNumericChannel() {
        assertEquals("number:42", new NotificationService().notifyNumber(42));
    }
}
