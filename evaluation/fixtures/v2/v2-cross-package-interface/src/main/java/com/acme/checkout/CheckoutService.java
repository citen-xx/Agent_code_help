package com.acme.checkout;

import com.acme.payment.PaymentGateway;

public class CheckoutService {
    private final PaymentGateway gateway;

    public CheckoutService(PaymentGateway gateway) {
        this.gateway = gateway;
    }

    public int checkout(int amount) {
        return gateway.charge(amount);
    }
}
