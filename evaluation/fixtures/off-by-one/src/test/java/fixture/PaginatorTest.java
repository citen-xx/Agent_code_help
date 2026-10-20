package fixture;

import static org.junit.Assert.assertEquals;
import org.junit.Test;

public class PaginatorTest {
    private final Paginator paginator = new Paginator();

    @Test
    public void countsPartialLastPage() {
        assertEquals(4, paginator.lastPage(10, 3));
    }

    @Test
    public void exactMultipleHasNoExtraPage() {
        assertEquals(3, paginator.lastPage(9, 3));
    }
}
