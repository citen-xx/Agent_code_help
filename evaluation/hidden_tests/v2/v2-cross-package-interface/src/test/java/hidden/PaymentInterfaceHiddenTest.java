package hidden;

import com.acme.checkout.CheckoutService;
import com.acme.payment.PaymentGateway;
import org.junit.Assert;
import org.junit.Test;

public class PaymentInterfaceHiddenTest {
    @Test
    public void usesPolymorphicGateway() {
        PaymentGateway gateway = amount -> amount + 5;
        Assert.assertEquals(105, new CheckoutService(gateway).checkout(100));
    }
}
