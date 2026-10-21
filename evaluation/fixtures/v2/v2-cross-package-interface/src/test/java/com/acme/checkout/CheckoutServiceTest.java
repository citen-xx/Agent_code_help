package com.acme.checkout;

import static org.junit.Assert.assertEquals;
import com.acme.payment.DefaultPaymentGateway;
import org.junit.Test;

public class CheckoutServiceTest {
    @Test
    public void addsPaymentFee() {
        assertEquals(
            1100,
            new CheckoutService(new DefaultPaymentGateway()).checkout(1000)
        );
    }
}
