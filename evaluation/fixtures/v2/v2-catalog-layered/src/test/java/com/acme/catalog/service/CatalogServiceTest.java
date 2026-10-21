package com.acme.catalog.service;

import com.acme.catalog.pricing.PricingPolicy;
import com.acme.catalog.repository.ProductRepository;
import org.junit.Assert;
import org.junit.Test;

public class CatalogServiceTest {
    @Test
    public void quotesSingleUnit() {
        Assert.assertEquals(
            110,
            new CatalogService(new ProductRepository(), new PricingPolicy()).quote("book", 1)
        );
    }
}
