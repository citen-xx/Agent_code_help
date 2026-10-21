package com.acme.payment;

public class DefaultPaymentGateway implements PaymentGateway {
    @Override
    public int charge(int amount) {
        return amount + amount / 100;
    }
}
