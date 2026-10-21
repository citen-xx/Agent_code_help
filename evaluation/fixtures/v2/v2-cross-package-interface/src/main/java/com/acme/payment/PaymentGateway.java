package com.acme.payment;

public interface PaymentGateway {
    int charge(int amount);
}
