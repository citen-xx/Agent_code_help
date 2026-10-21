package com.acme.catalog.controller;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class CatalogControllerTest {
    @Test
    public void quotesQuantityAndTax() {
        assertEquals(220, new CatalogController().quote("book", 2));
    }
}
