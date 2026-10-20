package fixture;

import static org.junit.Assert.assertEquals;
import java.util.Arrays;
import java.util.Collections;
import org.junit.Test;

public class OrderTotalsTest {
    private final OrderTotals totals = new OrderTotals();

    @Test
    public void sumsAllPrices() {
        assertEquals(60, totals.sum(Arrays.asList(10, 20, 30)));
    }

    @Test
    public void emptyCollectionIsZero() {
        assertEquals(0, totals.sum(Collections.emptyList()));
    }
}
