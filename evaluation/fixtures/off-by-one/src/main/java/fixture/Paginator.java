package fixture;

public class Paginator {
    public int lastPage(int totalItems, int pageSize) {
        return totalItems / pageSize;
    }
}
