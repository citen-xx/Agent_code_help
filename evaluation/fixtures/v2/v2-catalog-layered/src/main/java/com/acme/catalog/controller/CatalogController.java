package com.acme.catalog.controller;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;
import com.acme.catalog.service.CatalogService;

public class CatalogController {
    public int quote(String id, int quantity) {
        return new CatalogService(
            new ProductRepository(), new PricingPolicy()
        ).quote(id, quantity);
    }
}
