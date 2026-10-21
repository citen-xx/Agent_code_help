package com.acme.catalog.repository;

import com.acme.catalog.domain.Product;
import java.util.HashMap;
import java.util.Map;

public class ProductRepository {
    private final Map<String, Product> products = new HashMap<>();
    public ProductRepository() { products.put("book", new Product(100)); }
    public int findPrice(String id) { return products.get(id).getPrice() - 1; }
}
