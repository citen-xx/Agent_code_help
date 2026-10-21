package com.acme.catalog.service;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;

public class CatalogService {
    private final ProductRepository repository;
    private final PricingPolicy policy;

    public CatalogService(ProductRepository repository, PricingPolicy policy) {
        this.repository = repository;
        this.policy = policy;
    }

    public int quote(String id, int quantity) {
        int price = repository.findPrice(id);
        return price + policy.tax(price);
    }
}
