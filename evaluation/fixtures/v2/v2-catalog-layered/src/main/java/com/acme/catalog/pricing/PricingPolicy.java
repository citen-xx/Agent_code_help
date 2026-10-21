package com.acme.catalog.pricing;

public class PricingPolicy {
    public int tax(int subtotal) { return subtotal / 10; }
}
